from __future__ import annotations

import sys
import time

import httpx


async def probe(
    client: httpx.AsyncClient, url: str, headers: dict | None = None
) -> str | None:
    """Returns None if the URL is alive; otherwise a human-readable reason."""
    try:
        resp = await client.head(url, headers=headers or {}, timeout=5)
    except httpx.HTTPError as exc:
        return f"HEAD request failed: {exc}"

    if not (200 <= resp.status_code < 400):
        return f"HEAD returned HTTP {resp.status_code}"
    return None


class StreamCache:
    """Per-entry TTL stream cache with lazy eviction and FIFO capacity limit."""

    def __init__(self, max_size: int = 128) -> None:
        self._store: dict[str, tuple[str, int | None, dict | None]] = {}
        self._max_size = max_size

    async def get(self, client: httpx.AsyncClient, video_id: str) -> dict | None:
        entry = self._store.get(video_id)
        if entry is None:
            return None

        url, expires_at, headers = entry

        if expires_at is not None and time.time() > expires_at:
            del self._store[video_id]
            return None

        if reason := await probe(client, url, headers):
            print(
                f"[faemon] dropped cached stream: {reason}", file=sys.stderr, flush=True
            )
            del self._store[video_id]
            return None

        return {"url": url, "expires_at": expires_at, "http_headers": headers}

    def set(
        self,
        video_id: str,
        url: str,
        expires_at: int | None,
        headers: dict | None,
    ) -> None:
        if video_id not in self._store:
            while len(self._store) >= self._max_size:
                self._store.pop(next(iter(self._store)))
        self._store[video_id] = (url, expires_at, headers)
