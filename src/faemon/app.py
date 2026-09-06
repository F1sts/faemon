from __future__ import annotations

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .routes import routes as route_table
from .state import State


class _AuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, token: str):
        super().__init__(app)
        self._token = token

    async def dispatch(self, request: Request, call_next):
        if request.url.path in ("/health", "/proxy/stream"):
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if header != f"Bearer {self._token}":
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        return await call_next(request)


def create_app(token: str, state: State | None = None) -> Starlette:
    app = Starlette(
        routes=route_table,
        middleware=[
            Middleware(
                CORSMiddleware,
                allow_origins=["*"],
                allow_methods=["*"],
                allow_headers=["*"],
            ),
            Middleware(_AuthMiddleware, token=token),
        ],
    )
    app.state.faemon = state or State()
    return app
