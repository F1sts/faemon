from __future__ import annotations

import re
from time import perf_counter

from starlette.requests import Request

from ..helpers import err, get_state, ok, required
from ..services import lyrics as svc
from ..services.lyrics import LyricsError

_VIDEO_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")


async def lyrics(req: Request):
    raw_id, error = required(req, "id")
    if error:
        return err(error, 400)
    if _VIDEO_ID_RE.fullmatch(raw_id) is None:
        return err("Invalid video ID", 400)

    s = get_state(req)
    start = perf_counter()
    try:
        result = await svc.fetch(s.config, s.http, raw_id)
    except LyricsError as e:
        return err(str(e), 502)
    except Exception as e:
        return err(str(e))

    if result is None:
        return err(f"No lyrics found for {raw_id}", 404)

    return ok(result, round(perf_counter() - start, 4))
