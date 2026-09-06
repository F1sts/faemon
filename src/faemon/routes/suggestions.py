from __future__ import annotations

from time import perf_counter

from starlette.requests import Request

from ..helpers import err, get_state, ok, required
from ..services import suggestions as svc


async def suggestions(req: Request):
    q, error = required(req, "q")
    if error:
        return err(error, 400)

    state = get_state(req)
    start = perf_counter()
    try:
        result = await svc.fetch(state.http, q)
    except Exception as e:
        return err(str(e))

    return ok(result, round(perf_counter() - start, 4))
