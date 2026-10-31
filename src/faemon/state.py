from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from .cache import MatchCache, StreamCache


@dataclass
class Config:
    language: str = "en"
    location: str = ""


@dataclass
class State:
    config: Config = field(default_factory=Config)
    cache: StreamCache = field(default_factory=StreamCache)
    spotify_matches: MatchCache = field(default_factory=MatchCache)
    proxy_sessions: dict[str, str] = field(default_factory=dict)
    max_proxy_sessions: int = 20
    base_url: str = ""
    token: str = ""
    http: httpx.AsyncClient = field(
        default_factory=lambda: httpx.AsyncClient(follow_redirects=True, timeout=None)
    )
