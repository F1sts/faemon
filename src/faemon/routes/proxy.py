from __future__ import annotations

import secrets

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


async def stream_proxy(request: Request):
    video_id, error = required(request, "id")
    if error:
        return err(error, 400)

    state = get_state(request)

    sig = request.query_params.get("sig")
    if not sig or state.proxy_sessions.get(sig) != video_id:
        return err("Invalid signature", 401)

    try:
        api_resp = await state.http.get(
            f"{state.base_url}/yt/stream",
            params={"id": video_id},
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

    result = data.get("result") or {}
    stream_url = result.get("url")
    if not stream_url:
        return err("No stream URL found", 502)

    headers = (
        dict(result["http_headers"])
        if isinstance(result.get("http_headers"), dict)
        else {}
    )
    if range_header := request.headers.get("range"):
        headers["range"] = range_header

    try:
        upstream = await state.http.send(
            state.http.build_request("GET", stream_url, headers=headers),
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
