from __future__ import annotations

import asyncio
import secrets
import sys

import av
import certifi
import httpx
from starlette.requests import Request
from starlette.responses import StreamingResponse

from ..helpers import err, get_state, ok, required

_FORWARDED_UPSTREAM = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
)

_PCM_SAMPLE_RATE = 48000
_PCM_CHUNK_BYTES = _PCM_SAMPLE_RATE * 4 // 10
_PCM_TIMEOUT = (10.0, 30.0)
_PCM_MAX_RETRIES = 4


class _Retry(Exception):
    """Transient upstream failure; reopen and resume at `resume_at` seconds."""

    def __init__(self, cause: BaseException, resume_at: float) -> None:
        super().__init__(str(cause))
        self.resume_at = resume_at


async def sign_proxy(request: Request):
    video_id, error = required(request, "id")
    if error:
        return err(error, 400)

    state = get_state(request)
    nonce = secrets.token_urlsafe(32)

    while len(state.proxy_sessions) >= state.max_proxy_sessions:
        state.proxy_sessions.pop(next(iter(state.proxy_sessions)))

    state.proxy_sessions[nonce] = video_id
    return ok({"sig": nonce}, None)


async def _resolve_stream(state, video_id: str, fmt: str | None):
    """Returns {"url", "http_headers"} on success, else a JSONResponse error."""
    params: dict[str, str] = {"id": video_id}
    if fmt:
        params["format"] = fmt

    try:
        api_resp = await state.http.get(
            f"{state.base_url}/yt/stream",
            params=params,
            headers={"Authorization": f"Bearer {state.token}"},
        )
    except httpx.HTTPError as exc:
        return err(f"Stream resolution failed: {exc}", 502)

    data = api_resp.json()

    if api_resp.status_code != 200:
        msg = data.get("error") if isinstance(data, dict) else None
        return err(
            msg or f"Stream resolution failed (HTTP {api_resp.status_code})",
            api_resp.status_code,
        )

    stream_url = (result := data.get("result") or {}).get("url")
    if not stream_url:
        return err("No stream URL found", 502)

    headers = (
        dict(result["http_headers"])
        if isinstance(result.get("http_headers"), dict)
        else {}
    )
    return {"url": stream_url, "http_headers": headers}


def _invalid_sig(request: Request, video_id: str) -> str | None:
    sig = request.query_params.get("sig")
    if not sig:
        return "Missing required query parameter: sig"
    if request.app.state.faemon.proxy_sessions.get(sig) != video_id:
        return "Invalid signature"
    return None


async def stream_proxy(request: Request):
    video_id, error = required(request, "id")
    if error:
        return err(error, 400)

    if sig_error := _invalid_sig(request, video_id):
        return err(sig_error, 401)

    resolved = await _resolve_stream(get_state(request), video_id, None)
    if not isinstance(resolved, dict):
        return resolved

    headers = resolved["http_headers"]
    if range_header := request.headers.get("range"):
        headers["range"] = range_header

    state = get_state(request)
    # googlevideo closes pooled keep-alive connections after the cache's
    # liveness HEAD; the first GET on the stale connection dies with an
    # empty ReadError that one retry on a fresh connection heals.
    for attempt in (1, 2):
        try:
            upstream = await state.http.send(
                state.http.build_request("GET", resolved["url"], headers=headers),
                stream=True,
            )
            break
        except httpx.HTTPError as exc:
            if attempt == 2:
                return err(f"Upstream request failed: {exc}", 502)

    resp_headers = {
        name: upstream.headers[name]
        for name in _FORWARDED_UPSTREAM
        if name in upstream.headers
    }
    resp_headers["access-control-allow-origin"] = "*"

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        body(), status_code=upstream.status_code, headers=resp_headers
    )


def _open_pcm(url: str, headers: dict, start: float):
    options = {"headers": "\r\n".join(f"{k}: {v}" for k, v in headers.items())}
    if sys.platform == "linux":
        # The PyAV Linux wheels link GnuTLS, whose CA store is baked to the
        # RHEL-only /etc/pki/tls/cert.pem at build time. Point it at the
        # certifi bundle instead; macOS (SecureTransport) and Windows
        # (schannel) use the OS store and need no override.
        options["ca_file"] = certifi.where()
    container = av.open(url, options=options, timeout=_PCM_TIMEOUT)
    try:
        audio = next(s for s in container.streams if s.type == "audio")
    except StopIteration:
        container.close()
        raise ValueError("no audio stream")
    if start > 0:
        container.seek(int(round(start / audio.time_base)), stream=audio)
    return container, audio


def _pcm_frames(url: str, headers: dict, start: float, opened=None):
    """Yield 100 ms s16le chunks from `start` seconds.

    Raises _Retry on transient upstream read failures so the caller can
    reopen and resume; `opened` (from `_open_pcm`) skips the initial open.
    """
    try:
        container, audio = opened if opened is not None else _open_pcm(url, headers, start)
    except av.FFmpegError as exc:
        raise _Retry(exc, start) from None

    resampler = av.AudioResampler(
        format="s16", layout="stereo", rate=_PCM_SAMPLE_RATE
    )
    time_base = audio.time_base
    pending = bytearray()
    last_ts = start
    try:
        try:
            for frame in container.decode(audio):
                if time_base and frame.pts is not None:
                    last_ts = frame.pts * time_base
                    if last_ts < start:
                        continue
                for out in resampler.resample(frame):
                    pending += bytes(
                        memoryview(out.planes[0])[: out.samples * 4]
                    )
                    while len(pending) >= _PCM_CHUNK_BYTES:
                        yield bytes(pending[:_PCM_CHUNK_BYTES])
                        del pending[:_PCM_CHUNK_BYTES]
        except av.FFmpegError as exc:
            raise _Retry(exc, last_ts) from None
        for out in resampler.resample(None):
            pending += bytes(memoryview(out.planes[0])[: out.samples * 4])
        if pending:
            yield bytes(pending)
    finally:
        container.close()


async def pcm_proxy(request: Request):
    video_id, error = required(request, "id")
    if error:
        return err(error, 400)

    if sig_error := _invalid_sig(request, video_id):
        return err(sig_error, 401)

    t, t_error = required(request, "t")
    if t_error:
        return err(t_error, 400)

    try:
        start = float(t)
    except ValueError:
        return err("Parameter 't' must be a number", 400)
    if start < 0:
        return err("Parameter 't' must be >= 0", 400)

    state = get_state(request)
    fmt = request.query_params.get("format")
    resolved = await _resolve_stream(state, video_id, fmt)
    if not isinstance(resolved, dict):
        return resolved

    loop = asyncio.get_running_loop()
    try:
        opened = await loop.run_in_executor(
            None, _open_pcm, resolved["url"], resolved["http_headers"], start
        )
    except (av.FFmpegError, ValueError) as exc:
        return err(f"PCM open failed: {exc}", 502)

    start_pos = start
    failures = 0
    delivered = 0

    resp_headers = {
        "x-sample-rate": str(_PCM_SAMPLE_RATE),
        "x-channels": "2",
        "x-sample-format": "s16le",
    }
    # Real stream length beats the client's video-metadata duration: tracks
    # whose audio ends before the video must not look truncated.
    if opened[0].duration:
        resp_headers["x-duration"] = f"{opened[0].duration / 1_000_000:.3f}"

    print(
        f"[faemon] pcm start id={video_id} t={start:.1f}s"
        f" dur={opened[0].duration / 1_000_000:.3f}s"
        if opened[0].duration
        else f"[faemon] pcm start id={video_id} t={start:.1f}s dur=?",
        file=sys.stderr,
        flush=True,
    )

    def next_chunk(frames):
        try:
            return next(frames)
        except StopIteration:
            return None

    async def body():
        nonlocal resolved, start_pos, failures, delivered
        frames = _pcm_frames(
            resolved["url"], resolved["http_headers"], start_pos, opened
        )
        inflight: asyncio.Future | None = None
        try:
            while True:
                try:
                    inflight = loop.run_in_executor(None, next_chunk, frames)
                    chunk = await inflight
                    inflight = None
                except _Retry as retry:
                    failures += 1
                    if failures >= _PCM_MAX_RETRIES:
                        print(
                            f"[faemon] pcm failed id={video_id}"
                            f" after {_PCM_MAX_RETRIES - 1} retries"
                            f" at {start_pos:.1f}s: {retry}",
                            file=sys.stderr,
                            flush=True,
                        )
                        raise
                    start_pos = max(0.0, retry.resume_at)
                    print(
                        f"[faemon] pcm retry {failures}/{_PCM_MAX_RETRIES - 1}"
                        f" id={video_id} from {start_pos:.1f}s: {retry}",
                        file=sys.stderr,
                        flush=True,
                    )
                    fresh = await _resolve_stream(state, video_id, fmt)
                    if not isinstance(fresh, dict):
                        raise
                    resolved = fresh
                    await loop.run_in_executor(None, frames.close)
                    frames = _pcm_frames(
                        resolved["url"], resolved["http_headers"], start_pos
                    )
                    continue
                if chunk is None:
                    break
                failures = 0
                delivered += len(chunk)
                try:
                    yield chunk
                except GeneratorExit:
                    print(
                        f"[faemon] pcm client closed id={video_id}"
                        f" at ~{start + delivered / (_PCM_SAMPLE_RATE * 4):.1f}s"
                        f" ({delivered} bytes)",
                        file=sys.stderr,
                        flush=True,
                    )
                    raise
            print(
                f"[faemon] pcm complete id={video_id}"
                f" delivered={delivered} bytes"
                f" (~{start + delivered / (_PCM_SAMPLE_RATE * 4):.1f}s)",
                file=sys.stderr,
                flush=True,
            )
        finally:
            if inflight is not None:
                try:
                    await inflight
                except (av.FFmpegError, _Retry):
                    pass
            await loop.run_in_executor(None, frames.close)

    return StreamingResponse(
        body(),
        media_type="application/octet-stream",
        headers=resp_headers,
    )
