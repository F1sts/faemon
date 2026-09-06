from __future__ import annotations

import sys
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse

from .state import State


def ok(result, elapsed: float | None) -> JSONResponse:
    return JSONResponse({"result": result, "elapsed": elapsed})


def err(message: str, status: int = 500) -> JSONResponse:
    print(f"[faemon] error {status}: {message}", file=sys.stderr, flush=True)
    return JSONResponse({"error": message}, status_code=status)


def required(req: Request, name: str) -> tuple[str, str | None]:
    val = req.query_params.get(name)
    if not val:
        return "", f"Missing required query parameter: {name}"
    return val, None


def get_state(req: Request) -> State:
    return req.app.state.faemon


def view_count_str(val: Any) -> str | None:
    if val is None:
        return None
    if isinstance(val, int):
        return str(val)
    if isinstance(val, str):
        return val
    return None
