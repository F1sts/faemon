from __future__ import annotations

import asyncio
from time import perf_counter

from starlette.requests import Request

from ..helpers import err, get_state, ok, required
from ..services import ytm as svc


async def ytm_search(req: Request):
    q, error = required(req, "q")
    if error:
        return err(error, 400)

    limit = int(req.query_params.get("limit", "20"))
    filt = req.query_params.get("filter") or None

    s = get_state(req)
    start = perf_counter()
    try:
        results = await asyncio.to_thread(
            svc.search, s.config, q, filter=filt, limit=limit
        )
    except Exception as e:
        return err(str(e))

    return ok(results[:limit], round(perf_counter() - start, 4))


async def ytm_playlist(req: Request):
    raw_id, error = required(req, "id")
    if error:
        return err(error, 400)

    playlist_id = raw_id
    idx = raw_id.find("list=")
    if idx != -1:
        start = idx + 5
        end = raw_id.find("&", start)
        playlist_id = raw_id[start : end if end != -1 else len(raw_id)]

    s = get_state(req)
    start = perf_counter()
    try:
        parsed = await asyncio.to_thread(svc.playlist, s.config, playlist_id)
    except Exception as e:
        return err(str(e))

    if parsed is None:
        return err("Failed to parse YouTube Music playlist", 502)

    return ok(parsed, round(perf_counter() - start, 4))


async def ytm_album(req: Request):
    browse_id, error = required(req, "id")
    if error:
        return err(error, 400)

    s = get_state(req)
    start = perf_counter()
    try:
        parsed = await asyncio.to_thread(svc.album, s.config, browse_id)
    except Exception as e:
        return err(str(e))

    if parsed is None:
        return err("Failed to parse YouTube Music album", 502)

    return ok(parsed, round(perf_counter() - start, 4))


async def ytm_artist(req: Request):
    channel_id, error = required(req, "id")
    if error:
        return err(error, 400)

    s = get_state(req)
    start = perf_counter()
    try:
        parsed = await asyncio.to_thread(svc.artist, s.config, channel_id)
    except Exception as e:
        return err(str(e))

    if parsed is None:
        return err("Failed to parse YouTube Music artist", 502)

    return ok(parsed, round(perf_counter() - start, 4))


async def ytm_watch_playlist(req: Request):
    wid, error = required(req, "id")
    if error:
        return err(error, 400)

    limit = int(req.query_params.get("limit", "25"))
    radio = req.query_params.get("radio", "").lower() in ("1", "true")
    shuffle = req.query_params.get("shuffle", "").lower() in ("1", "true")

    s = get_state(req)
    start = perf_counter()
    try:
        results = await asyncio.to_thread(
            svc.watch_playlist,
            s.config,
            wid,
            limit=limit,
            radio=radio,
            shuffle=shuffle,
        )
    except Exception as e:
        return err(str(e))

    return ok(results[:limit], round(perf_counter() - start, 4))
