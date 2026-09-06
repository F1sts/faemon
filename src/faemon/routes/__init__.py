from __future__ import annotations

from starlette.routing import Route

from ..helpers import ok
from .proxy import sign_proxy, stream_proxy
from .suggestions import suggestions
from .yt import yt_extract, yt_playlist, yt_search, yt_stream
from .ytm import ytm_album, ytm_artist, ytm_playlist, ytm_search, ytm_watch_playlist


async def health(request):
    return ok({"status": "ok"}, None)


routes = [
    Route("/health", health),
    Route("/yt/search", yt_search),
    Route("/yt/playlist", yt_playlist),
    Route("/yt/stream", yt_stream),
    Route("/yt/extract", yt_extract, methods=["POST"]),
    Route("/ytm/search", ytm_search),
    Route("/ytm/playlist", ytm_playlist),
    Route("/ytm/album", ytm_album),
    Route("/ytm/artist", ytm_artist),
    Route("/ytm/watch-playlist", ytm_watch_playlist),
    Route("/suggestions", suggestions),
    Route("/proxy/sign", sign_proxy),
    Route("/proxy/stream", stream_proxy),
]
