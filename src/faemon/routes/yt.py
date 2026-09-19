from __future__ import annotations

import asyncio
import sys
import urllib.parse
from time import perf_counter

from starlette.requests import Request

from ..cache import probe
from ..helpers import err, get_state, ok, required
from ..services import yt as svc


async def yt_search(req: Request):
    q, error = required(req, "q")
    if error:
        return err(error, 400)

    limit = int(req.query_params.get("limit", "20"))
    stype = req.query_params.get("type", "all")

    sp_map = {
        "all": "",
        "videos": "&sp=EgIQAQ%253D%253D",
        "playlists": "&sp=EgIQAw%253D%253D",
        "channels": "&sp=EgIQAg%253D%253D",
    }
    sp = sp_map.get(stype, "")

    encoded = urllib.parse.quote(q, safe="")
    search_url = f"https://www.youtube.com/results?search_query={encoded}{sp}"

    opts = {
        "extract_flat": True,
        "dump_single_json": True,
        "playlistend": limit,
    }

    start = perf_counter()
    try:
        raw = await asyncio.to_thread(svc.extract, search_url, opts)
    except Exception as e:
        return err(str(e))

    entries = raw.get("entries") or []
    results = [r for e in entries if (r := svc.parse_entry(e)) is not None]

    return ok(results[:limit], round(perf_counter() - start, 4))


async def yt_playlist(req: Request):
    raw_id, error = required(req, "id")
    if error:
        return err(error, 400)

    url = (
        raw_id
        if raw_id.startswith("http")
        else f"https://www.youtube.com/playlist?list={raw_id}"
    )

    opts = {"extract_flat": "in_playlist", "dump_single_json": True}

    start = perf_counter()
    try:
        raw = await asyncio.to_thread(svc.extract, url, opts)
    except Exception as e:
        return err(str(e))

    parsed = svc.parse_playlist(raw)
    if parsed is None:
        return err("Failed to parse YouTube playlist", 502)

    return ok(parsed, round(perf_counter() - start, 4))


async def yt_stream(req: Request):
    video_id, error = required(req, "id")
    if error:
        return err(error, 400)

    s = get_state(req)

    cached = await s.cache.get(s.http, video_id)
    if cached is not None:
        return ok(cached, None)

    video_url = f"https://www.youtube.com/watch?v={video_id}"

    # Windows WebView2 (Chromium) seeks WebM fine; macOS/Linux WebKit
    # (AVFoundation) cannot range-seek WebM and needs M4A instead.
    audio_format = (
        "bestaudio/best" if sys.platform == "win32" else "bestaudio[ext=m4a]/bestaudio/best"
    )

    opts = {
        "format": audio_format,
        "extract_flat": True,
        "extractor_retries": 3,
        "retries": 5,
        "fragment_retries": 3,
        "skip_download": True,
        "writesubtitles": False,
        "writeautomaticsub": False,
        "getcomments": False,
        "extractor_args": {},
    }

    start = perf_counter()
    reasons: list[str] = []
    for _ in range(3):
        try:
            raw = await asyncio.to_thread(svc.extract, video_url, opts)
        except Exception as e:
            return err(str(e))

        stream_url = svc.parse_stream_url(raw)
        if not stream_url:
            return err("yt-dlp returned an empty stream URL", 502)

        expires_at = svc.extract_expiration(stream_url)
        http_headers = raw.get("http_headers")
        stream = {
            "url": stream_url,
            "expires_at": expires_at,
            "http_headers": http_headers if isinstance(http_headers, dict) else None,
        }

        if reason := await probe(s.http, stream_url, stream["http_headers"]):
            reasons.append(reason)
            await asyncio.sleep(0.15)
            continue

        s.cache.set(video_id, stream_url, expires_at, stream["http_headers"])
        return ok(stream, round(perf_counter() - start, 4))

    return err(f"Stream URL did not pass liveness check: {'; '.join(reasons)}", 502)


async def yt_extract(req: Request):
    try:
        body = await req.json()
    except Exception:
        return err("Invalid JSON body", 400)

    query = body.get("query")
    if not query:
        return err("Missing 'query' in body", 400)

    options = body.get("options") or {}

    start = perf_counter()
    try:
        raw = await asyncio.to_thread(svc.extract, query, options)
    except Exception as e:
        return err(str(e))

    return ok(raw, round(perf_counter() - start, 4))
