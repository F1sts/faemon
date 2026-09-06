from __future__ import annotations

import re
from typing import Any

from yt_dlp import YoutubeDL

from ..helpers import view_count_str


def extract(query: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
    opts = {**(options or {}), "quiet": True, "no_warnings": True}
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(query, download=False)


def parse_entry(entry: dict[str, Any]) -> dict[str, Any] | None:
    eid = entry.get("id")
    if not eid or not isinstance(eid, str):
        return None
    title = entry.get("title")
    if not title or not isinstance(title, str):
        return None

    raw_type = entry.get("_type", "unknown")
    url = entry.get("url")

    result_type = raw_type
    if isinstance(url, str):
        if "/playlist?list=" in url:
            result_type = "playlist"
        elif "/channel/" in url or "/@" in url:
            result_type = "channel"
        elif "/watch?v=" in url:
            result_type = "video"

    thumbnails = entry.get("thumbnails")
    if not isinstance(thumbnails, list):
        thumbnails = []

    return {
        "id": eid,
        "title": title,
        "type": result_type,
        "url": url if isinstance(url, str) else None,
        "description": entry.get("description")
        if isinstance(entry.get("description"), str)
        else None,
        "duration": entry.get("duration")
        if isinstance(entry.get("duration"), (int, float))
        else None,
        "view_count": view_count_str(entry.get("view_count")),
        "artists": _parse_authors(entry),
        "album": None,
        "is_explicit": None,
        "thumbnails": thumbnails,
    }


def parse_playlist(response: dict[str, Any]) -> dict[str, Any] | None:
    pid = response.get("id")
    if not isinstance(pid, str):
        return None
    title = response.get("title")
    if not isinstance(title, str):
        return None

    thumbnails = response.get("thumbnails")
    if not isinstance(thumbnails, list):
        thumbnails = []

    entries = [
        r for e in (response.get("entries") or []) if (r := parse_entry(e)) is not None
    ]

    return {
        "id": pid,
        "title": title,
        "availability": response.get("availability")
        if isinstance(response.get("availability"), str)
        else None,
        "description": response.get("description")
        if isinstance(response.get("description"), str)
        else None,
        "authors": _parse_authors(response),
        "thumbnails": thumbnails,
        "entries": entries,
        "year": str(v)
        if isinstance(v := response.get("release_year"), (int, str))
        else None,
        "view_count": view_count_str(response.get("view_count")),
        "track_count": response.get("playlist_count")
        if isinstance(response.get("playlist_count"), int)
        else None,
        "browse_id": None,
        "other_versions": None,
        "related_recommendations": None,
    }


def parse_stream_url(response: dict[str, Any]) -> str | None:
    direct = response.get("url")
    if isinstance(direct, str) and direct:
        return direct

    formats = response.get("formats")
    if not isinstance(formats, list):
        return None

    best: dict[str, Any] | None = None
    best_abr: float = 0.0
    for fmt in formats:
        vcodec = fmt.get("vcodec", "")
        acodec = fmt.get("acodec", "none")
        if vcodec != "none" or acodec == "none":
            continue
        abr = float(fmt["abr"]) if isinstance(fmt.get("abr"), (int, float)) else 0.0
        if abr > best_abr:
            best_abr = abr
            best = fmt

    if best is None:
        return None
    url = best.get("url")
    return url if isinstance(url, str) else None


def extract_expiration(url: str) -> int | None:
    m = re.search(r"[?&]expire=(\d+)", url)
    return int(m.group(1)) if m else None


def _parse_authors(entry: dict[str, Any]) -> list[dict[str, Any]]:
    uploader = entry.get("uploader")
    if isinstance(uploader, str):
        return [
            {
                "name": uploader,
                "id": entry.get("uploader_id")
                if isinstance(entry.get("uploader_id"), str)
                else None,
            }
        ]
    channel = entry.get("channel")
    if isinstance(channel, str):
        return [
            {
                "name": channel,
                "id": entry.get("channel_id")
                if isinstance(entry.get("channel_id"), str)
                else None,
            }
        ]
    return []
