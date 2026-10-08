from __future__ import annotations

import asyncio
import secrets

import av
import httpx
from starlette.requests import Request
from starlette.responses import StreamingResponse

from ..helpers import err, get_state, ok, required
from ..mp4patch import patcher_for

_FORWARDED_UPSTREAM = (
    "content-type",
    "content-length",
    "content-range",
    "accept-ranges",
)

_PCM_SAMPLE_RATE = 48000
_PCM_CHUNK_BYTES = _PCM_SAMPLE_RATE * 4 // 10
_PCM_TIMEOUT = (10.0, 30.0)


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
    try:
        upstream = await state.http.send(
            state.http.build_request("GET", resolved["url"], headers=headers),
            stream=True,
        )
    except httpx.HTTPError as exc:
        return err(f"Upstream request failed: {exc}", 502)

    resp_headers = {
        name: upstream.headers[name]
        for name in _FORWARDED_UPSTREAM
        if name in upstream.headers
    }
    resp_headers["access-control-allow-origin"] = "*"

    patcher = patcher_for(
        upstream.headers.get("content-type", ""),
        upstream.headers.get("content-range", ""),
    )

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                if patcher is not None:
                    chunk = patcher.feed(chunk)
                    if chunk is None:
                        continue
                yield chunk
        finally:
            if patcher is not None and (tail := patcher.flush()):
                yield tail
            await upstream.aclose()

    return StreamingResponse(
        body(), status_code=upstream.status_code, headers=resp_headers
    )


def _open_pcm(url: str, headers: dict, start: float):
    options = {"headers": "\r\n".join(f"{k}: {v}" for k, v in headers.items())}
    container = av.open(url, options=options, timeout=_PCM_TIMEOUT)
    try:
        audio = next(s for s in container.streams if s.type == "audio")
    except StopIteration:
        container.close()
        raise ValueError("no audio stream")
    if start > 0:
        container.seek(int(round(start / audio.time_base)), stream=audio)
    return container, audio


def _pcm_frames(container, audio, start: float):
    resampler = av.AudioResampler(
        format="s16", layout="stereo", rate=_PCM_SAMPLE_RATE
    )
    time_base = audio.time_base
    pending = bytearray()
    try:
        for frame in container.decode(audio):
            if time_base and frame.pts is not None and frame.pts * time_base < start:
                continue
            for out in resampler.resample(frame):
                pending += bytes(
                    memoryview(out.planes[0])[: out.samples * 4]
                )
                while len(pending) >= _PCM_CHUNK_BYTES:
                    yield bytes(pending[:_PCM_CHUNK_BYTES])
                    del pending[:_PCM_CHUNK_BYTES]
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

    resolved = await _resolve_stream(
        get_state(request), video_id, request.query_params.get("format")
    )
    if not isinstance(resolved, dict):
        return resolved

    loop = asyncio.get_running_loop()
    try:
        container, audio = await loop.run_in_executor(
            None, _open_pcm, resolved["url"], resolved["http_headers"], start
        )
    except (av.FFmpegError, ValueError) as exc:
        return err(f"PCM open failed: {exc}", 502)

    frames = _pcm_frames(container, audio, start)

    def next_chunk():
        try:
            return next(frames)
        except StopIteration:
            return None

    async def body():
        inflight: asyncio.Future | None = None
        try:
            while True:
                inflight = loop.run_in_executor(None, next_chunk)
                chunk = await inflight
                inflight = None
                if chunk is None:
                    break
                yield chunk
        finally:
            if inflight is not None:
                try:
                    await inflight
                except av.FFmpegError:
                    pass
            await loop.run_in_executor(None, frames.close)

    return StreamingResponse(
        body(),
        media_type="application/octet-stream",
        headers={
            "x-sample-rate": str(_PCM_SAMPLE_RATE),
            "x-channels": "2",
            "x-sample-format": "s16le",
        },
    )
