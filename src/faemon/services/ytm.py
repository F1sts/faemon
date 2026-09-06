from __future__ import annotations

from typing import Any

from ytmusicapi import YTMusic

from ..helpers import view_count_str
from ..state import Config


def _make_ytm(config: Config) -> YTMusic:
    return YTMusic(language=config.language, location=config.location)


# ── Search ──────────────────────────────────────────────────────────────────


def search(
    config: Config,
    query: str,
    *,
    filter: str | None = None,
    limit: int = 20,
) -> list[dict]:
    with _make_ytm(config) as ytm:
        raw = ytm.search(
            query=query,
            filter=filter,
            limit=limit,
            ignore_spelling=False,
        )
    return [r for e in raw if (r := _parse_entry(e)) is not None]


# ── Playlist ────────────────────────────────────────────────────────────────


def playlist(config: Config, playlist_id: str) -> dict | None:
    with _make_ytm(config) as ytm:
        raw = ytm.get_playlist(
            playlistId=playlist_id, limit=None, related=False, suggestions_limit=0
        )
    return _parse_playlist(raw)


# ── Album ───────────────────────────────────────────────────────────────────


def album(config: Config, browse_id: str) -> dict | None:
    with _make_ytm(config) as ytm:
        raw = ytm.get_album(browseId=browse_id)
    return _parse_album(raw, browse_id)


# ── Artist ──────────────────────────────────────────────────────────────────


def artist(config: Config, channel_id: str) -> dict | None:
    with _make_ytm(config) as ytm:
        raw = ytm.get_artist(channelId=channel_id)
    return _parse_artist(raw)


# ── Watch Playlist ──────────────────────────────────────────────────────────


def watch_playlist(
    config: Config,
    id: str,
    *,
    limit: int = 25,
    radio: bool = False,
    shuffle: bool = False,
) -> list[dict]:
    kwargs: dict[str, Any] = {
        "limit": limit,
        "radio": radio,
        "shuffle": shuffle,
    }
    if len(id) == 11:
        kwargs["videoId"] = id
    else:
        kwargs["playlistId"] = id

    with _make_ytm(config) as ytm:
        raw = ytm.get_watch_playlist(**kwargs)

    tracks = raw.get("tracks") or []
    results = [_parse_watch_track(t) for t in tracks]
    return [r for r in results if r is not None]


# ── Parsers ─────────────────────────────────────────────────────────────────


def _parse_entry(entry: dict) -> dict | None:
    result_type = entry.get("resultType") or (entry.get("videoId") and "song")
    if result_type is None:
        return None

    vid = entry.get("videoId")
    bid = entry.get("browseId")
    pid = entry.get("playlistId")
    raw_id = vid or bid or pid
    if raw_id is None:
        return None

    title = entry.get("title") or entry.get("artist")
    if not isinstance(title, str):
        return None

    artists = _parse_artists_array(entry)
    if not artists:
        single = entry.get("artist") or entry.get("author")
        if isinstance(single, str):
            artists = [{"name": single, "id": None}]

    album_raw = entry.get("album")
    album = (
        {"name": album_raw.get("name", ""), "id": album_raw.get("id")}
        if isinstance(album_raw, dict)
        else None
    )

    return {
        "id": raw_id,
        "title": title,
        "type": result_type,
        "url": None,
        "description": None,
        "duration": entry.get("duration_seconds"),
        "view_count": view_count_str(entry.get("views")),
        "artists": artists,
        "album": album,
        "is_explicit": entry.get("isExplicit"),
        "thumbnails": _parse_thumbnails(entry.get("thumbnails")),
    }


def _parse_playlist(raw: dict) -> dict | None:
    pid = raw.get("id")
    title = raw.get("title")
    if pid is None or title is None:
        return None

    authors: list[dict] = []
    author = raw.get("author")
    if isinstance(author, dict):
        name = author.get("name")
        if isinstance(name, str):
            authors = [{"name": name, "id": author.get("id")}]

    tracks = raw.get("tracks") or []
    entries = [_parse_entry(t) for t in tracks]
    entries = [e for e in entries if e is not None]

    return {
        "id": pid,
        "title": title,
        "availability": raw.get("privacy"),
        "description": raw.get("description"),
        "authors": authors,
        "thumbnails": _parse_thumbnails(raw.get("thumbnails")),
        "entries": entries,
        "year": raw.get("year"),
        "view_count": view_count_str(raw.get("views")),
        "track_count": _int_or_none(raw.get("trackCount")),
        "browse_id": None,
        "other_versions": None,
        "related_recommendations": None,
    }


def _parse_album(raw: dict, browse_id: str) -> dict | None:
    title = raw.get("title")
    if title is None:
        return None

    pid = raw.get("audioPlaylistId") or browse_id

    artists = _parse_artists_array(raw)

    tracks = raw.get("tracks") or []
    entries = [_parse_entry(t) for t in tracks]
    entries = [e for e in entries if e is not None]

    other = raw.get("other_versions")
    other_versions = (
        [_parse_album(v, "") for v in other if isinstance(v, dict)]
        if isinstance(other, list)
        else None
    )
    if other_versions is not None:
        other_versions = [o for o in other_versions if o is not None] or None

    related = raw.get("related_recommendations")
    related_recs = (
        [_parse_album(v, "") for v in related if isinstance(v, dict)]
        if isinstance(related, list)
        else None
    )
    if related_recs is not None:
        related_recs = [r for r in related_recs if r is not None] or None

    bid = raw.get("browseId") or (browse_id or None)

    return {
        "id": pid,
        "title": title,
        "availability": None,
        "description": raw.get("description"),
        "authors": artists,
        "thumbnails": _parse_thumbnails(raw.get("thumbnails")),
        "entries": entries,
        "year": raw.get("year"),
        "view_count": None,
        "track_count": _int_or_none(raw.get("trackCount")),
        "browse_id": bid,
        "other_versions": other_versions,
        "related_recommendations": related_recs,
    }


def _parse_artist(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None

    sections = ("songs", "albums", "singles", "videos", "related")
    for key in sections:
        val = raw.get(key)
        if isinstance(val, dict) and "results" in val:
            val["results"] = [v for v in val["results"] if isinstance(v, dict)]

    return {
        "name": raw.get("name"),
        "description": raw.get("description"),
        "views": raw.get("views"),
        "channelId": raw.get("channelId"),
        "shuffleId": raw.get("shuffleId"),
        "radioId": raw.get("radioId"),
        "subscribers": raw.get("subscribers"),
        "monthlyListeners": raw.get("monthlyListeners"),
        "subscribed": raw.get("subscribed", False),
        "thumbnails": _parse_thumbnails(raw.get("thumbnails")),
        "songs": raw.get("songs"),
        "albums": raw.get("albums"),
        "singles": raw.get("singles"),
        "videos": raw.get("videos"),
        "related": raw.get("related"),
    }


def _parse_watch_track(track: dict) -> dict | None:
    vid = track.get("videoId")
    title = track.get("title")
    if vid is None or title is None:
        return None

    artists = _parse_artists_array(track)

    album_raw = track.get("album")
    album = (
        {"name": album_raw.get("name", ""), "id": album_raw.get("id")}
        if isinstance(album_raw, dict)
        else None
    )

    return {
        "id": vid,
        "title": title,
        "type": "song",
        "url": None,
        "description": None,
        "duration": _parse_length(track.get("length")),
        "view_count": None,
        "artists": artists,
        "album": album,
        "is_explicit": None,
        "thumbnails": _parse_thumbnails(track.get("thumbnail")),
    }


# ── Helpers ─────────────────────────────────────────────────────────────────


def _parse_artists_array(obj: dict) -> list[dict]:
    arr = obj.get("artists")
    if not isinstance(arr, list):
        return []
    out: list[dict] = []
    for a in arr:
        if isinstance(a, dict):
            name = a.get("name")
            if isinstance(name, str):
                out.append({"name": name, "id": a.get("id")})
    return out


def _parse_thumbnails(val: Any) -> list[dict]:
    if not isinstance(val, list):
        return []
    out: list[dict] = []
    for t in val:
        if isinstance(t, dict) and "url" in t:
            out.append(
                {
                    "url": t["url"],
                    "width": t.get("width", 0),
                    "height": t.get("height", 0),
                }
            )
    return out


def _parse_length(length: str | None) -> float | None:
    if not isinstance(length, str):
        return None
    parts = length.split(":")
    try:
        if len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
    except ValueError:
        pass
    return None


def _int_or_none(val: Any) -> int | None:
    if isinstance(val, int):
        return val
    return None
