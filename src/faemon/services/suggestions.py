from __future__ import annotations

import urllib.parse

import httpx

_SUGGEST_URL = "https://suggestqueries.google.com/complete/search"


async def fetch(client: httpx.AsyncClient, query: str) -> list[str]:
    encoded = urllib.parse.quote(query, safe="")
    url = f"{_SUGGEST_URL}?client=firefox&ds=yt&ie=utf-8&oe=utf-8&q={encoded}"
    resp = await client.get(url, timeout=5)
    parsed = resp.json()
    arr = parsed[1] if isinstance(parsed, list) and len(parsed) > 1 else []
    return [s for s in arr if isinstance(s, str)]
