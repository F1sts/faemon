from __future__ import annotations

import asyncio
import json
import queue
import threading
from time import perf_counter

from starlette.requests import Request
from starlette.responses import StreamingResponse

from ..helpers import err, get_state, ok, required
from ..services import spotify as svc
from ..services.spotify import SpotifyError, _Cancelled
from ..state import State


async def spotify_match(req: Request):
    s = get_state(req)
    try:
        body = await req.json()
    except json.JSONDecodeError:
        return err("Request body must be JSON", 400)

    tracks = body.get("tracks") if isinstance(body, dict) else None
    if not isinstance(tracks, list) or not tracks:
        return err("Body must contain a non-empty 'tracks' list", 400)

    start = perf_counter()
    try:
        result = await asyncio.to_thread(svc.match, s, tracks)
    except ValueError as e:
        return err(str(e), 400)
    except Exception as e:
        return err(str(e))

    return ok(result, round(perf_counter() - start, 4))


async def spotify_playlist(req: Request):
    raw_id, error = required(req, "id")
    if error:
        return err(error, 400)

    s = get_state(req)
    start = perf_counter()
    if req.query_params.get("stream", "").lower() in ("1", "true"):
        return _stream(s, raw_id, start)

    try:
        parsed = await asyncio.to_thread(svc.playlist, s, raw_id)
    except SpotifyError as e:
        return err(str(e), e.status)
    except ValueError as e:
        return err(str(e), 400)
    except Exception as e:
        return err(str(e))

    return ok(parsed, round(perf_counter() - start, 4))


def _stream(s: State, raw_id: str, start: float) -> StreamingResponse:
    events: queue.Queue[dict | None] = queue.Queue()
    cancel = threading.Event()

    def on_progress(stage: str, completed: int, total: int) -> None:
        events.put({"stage": stage, "completed": completed, "total": total})

    def run() -> None:
        try:
            result = svc.playlist(s, raw_id, on_progress, cancel)
        except _Cancelled:
            pass
        except SpotifyError as e:
            events.put({"error": str(e), "status": e.status})
        except ValueError as e:
            events.put({"error": str(e), "status": 400})
        except Exception as e:
            events.put({"error": str(e)})
        else:
            events.put({"result": result, "elapsed": round(perf_counter() - start, 4)})
        finally:
            events.put(None)

    asyncio.get_running_loop().run_in_executor(None, run)
    return StreamingResponse(
        _stream_events(events, cancel),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache"},
    )


async def _stream_events(events: queue.Queue[dict | None], cancel: threading.Event):
    try:
        while True:
            event = await asyncio.to_thread(events.get)
            if event is None:
                return
            yield json.dumps(event, ensure_ascii=False) + "\n"
    finally:
        # A closed connection (modal closed, aborted fetch, app quit) stops
        # the import instead of leaving a zombie draining rate limits.
        cancel.set()
