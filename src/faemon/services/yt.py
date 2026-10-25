from __future__ import annotations

import re
from typing import Any

import httpx
from yt_dlp import YoutubeDL

from ..helpers import view_count_str


class FastAudioResolver:
    """
    Sub-200ms YouTube stream resolver using spatial/VR InnerTube clients.
    Acts as a high-speed first-pass before falling back to yt-dlp.
    """

    INNERTUBE_URL = "https://www.youtube.com/youtubei/v1/player"

    # Priority cascade: VisionOS (fastest, Opus 251) -> Quest VR (Opus 251) -> iOS (AAC 140)
    CLIENT_CASCADE = [
        {"clientName": "VISIONOS", "clientVersion": "0.1"},
        {"clientName": "ANDROID_VR", "clientVersion": "1.61.47"},
        {"clientName": "IOS", "clientVersion": "21.41.5"},
    ]

    def __init__(self):
        # Persistent HTTP/2 client keeps connection/TLS warm across calls
        self.http = httpx.Client(
            http2=True,
            timeout=2.5,  # Fail fast to trigger fallback if stalled
            headers={
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15",
            },
        )
        self.visitor_data: str | None = None

    def resolve(self, video_id: str) -> str | None:
        """
        Attempts to resolve a direct, unthrottled audio stream URL.
        Returns the CDN URL string on success, or None on failure.
        """
        # A fresh session has no visitorData, and YouTube rejects its first
        # request ("Sign in to confirm you're not a bot") while still handing
        # back a visitorData. Retry the cascade once with it rather than
        # falling through to yt-dlp on every cold start.
        for _ in range(2):
            visitor_before = self.visitor_data

            for client_info in self.CLIENT_CASCADE:
                try:
                    context = {
                        "client": {
                            **client_info,
                            "hl": "en",
                            "gl": "US",
                        }
                    }
                    if self.visitor_data:
                        context["client"]["visitorData"] = self.visitor_data

                    payload = {
                        "videoId": video_id,
                        "context": context
                    }

                    resp = self.http.post(self.INNERTUBE_URL, json=payload)
                    if resp.status_code != 200:
                        continue

                    # Update session visitorData for subsequent requests
                    header_visitor = resp.headers.get("X-YouTube-Visitor-Data")
                    if header_visitor:
                        self.visitor_data = header_visitor

                    data = resp.json()
                    body_visitor = data.get("responseContext", {}).get("visitorData")
                    if body_visitor:
                        self.visitor_data = body_visitor

                    formats = data.get("streamingData", {}).get("adaptiveFormats", [])

                    # 1. Best: ITag 251 (Opus ~160kbps)
                    for f in formats:
                        if f.get("itag") == 251 and f.get("url"):
                            return f["url"]

                    # 2. Alternative: ITag 140 (AAC ~128kbps)
                    for f in formats:
                        if f.get("itag") == 140 and f.get("url"):
                            return f["url"]

                    # 3. Fallback: Any direct audio stream
                    for f in formats:
                        if "audio" in f.get("mimeType", "") and f.get("url"):
                            return f["url"]

                except Exception:
                    # On timeout, connection drop, or schema change, try next client
                    continue

            if self.visitor_data is None or self.visitor_data == visitor_before:
                break

        # All fast clients failed -> signals caller to invoke yt-dlp
        return None


# Module-level singleton to maintain warm HTTP/2 connections in long-running processes
_resolver_instance = FastAudioResolver()


def get_fast_audio_url(video_id: str) -> str | None:
    """Public helper for one-line imports."""
    return _resolver_instance.resolve(video_id)


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
