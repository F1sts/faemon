from __future__ import annotations

import json
import re
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx
from ytmusicapi import YTMusic

from ..state import Config
from .ytm import make_ytm, parse_entry

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
_NEXT_DATA_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.DOTALL
)
_PLAYLIST_ID_RE = re.compile(r"playlist[/:]([A-Za-z0-9]{22})")
_BARE_PLAYLIST_ID_RE = re.compile(r"[A-Za-z0-9]{22}")
_FEATURING_RE = re.compile(
    r"\s*[\(\[](?:feat\.?|ft\.?|with)[^)\]]*[\)\]]", re.IGNORECASE
)
_VARIANT_RE = re.compile(
    r"\s*[\(\[](?:[^)\]]*\b(?:remaster(?:ed)?|version|live|mono|deluxe|edit|remix|"
    r"mix|bonus|acoustic|demo|anniversary)\b[^)\]]*)[\)\]]",
    re.IGNORECASE,
)
_VARIANT_SUFFIX_RE = re.compile(
    r"\s+-\s+.*\b(?:remaster(?:ed)?|version|live|mono|deluxe|edit|remix|mix|"
    r"bonus|acoustic|demo|anniversary)\b.*$",
    re.IGNORECASE,
)

_TRACK_PREFIX = "spotify:track:"
_ARTIST_SEPARATOR_RE = re.compile(
    r"\s*(?:&|\+|\band\b|\bwith\b)\s*", re.IGNORECASE
)
_WORKERS = 8

_NOISE_WORDS = {
    "album",
    "anniversary",
    "audio",
    "bonus",
    "clean",
    "deluxe",
    "edition",
    "explicit",
    "hd",
    "hq",
    "lyric",
    "mono",
    "official",
    "original",
    "radio",
    "remaster",
    "remastered",
    "single",
    "stereo",
    "track",
    "ver",
    "version",
    "video",
    "visualizer",
    "4k",
}
_IDENTITY_WORDS = {
    "8d",
    "acoustic",
    "demo",
    "douyin",
    "edit",
    "extended",
    "hardstyle",
    "instrumental",
    "jumpstyle",
    "live",
    "mix",
    "nightcore",
    "remix",
    "reverb",
    "slowed",
    "sped",
    "speed",
    "tiktok",
}
_QUALIFIER_WORDS = _NOISE_WORDS | _IDENTITY_WORDS
_MIN_TITLE_SCORE = 0.4
_NEAR_EXACT_TITLE_SCORE = 0.95
_ARTIST_WEIGHT = 2.0
_ARTIST_OVERLAP_WEIGHT = 0.1
_TITLE_WEIGHT = 2.0
_DURATION_WEIGHT = 0.5
_DURATION_HORIZON_SEC = 30.0

ProgressCallback = Callable[[str, int, int], None]


class SpotifyError(Exception):
    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


def playlist(
    config: Config,
    url_or_id: str,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    playlist_id = _extract_playlist_id(url_or_id)
    if on_progress:
        on_progress("extracting", 0, 0)

    source = _fetch_playlist(playlist_id, on_progress)
    if on_progress:
        on_progress("matching", 0, len(source["tracks"]))

    entries = _match_tracks(config, source["tracks"], on_progress)

    return {
        "id": playlist_id,
        "title": source["title"],
        "availability": None,
        "description": None,
        "authors": [{"name": source["owner"], "id": None}] if source["owner"] else [],
        "thumbnails": source["thumbnails"],
        "entries": entries,
        "year": None,
        "view_count": None,
        "track_count": source["total"],
        "browse_id": None,
        "other_versions": None,
        "related_recommendations": None,
        "matched_count": len(entries),
    }


# ── Spotify extraction ──────────────────────────────────────────────────────


def _get(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 15.0,
) -> httpx.Response:
    response: httpx.Response | None = None
    for attempt in (1, 2):
        try:
            response = client.get(url, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            if attempt == 2:
                raise SpotifyError(f"Could not reach Spotify: {exc}") from exc
            continue

        if response.status_code < 500 and response.status_code != 429:
            return response
        if attempt == 1:
            time.sleep(0.5)

    if response is None:
        raise SpotifyError("Could not reach Spotify")
    return response


def _fetch_playlist(
    playlist_id: str,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    with httpx.Client(
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=True,
        timeout=15.0,
    ) as client:
        resp = _get(client, f"https://open.spotify.com/embed/playlist/{playlist_id}")
        if resp.status_code != 200:
            raise SpotifyError(
                f"Spotify returned HTTP {resp.status_code} for this playlist"
            )

        state = _page_state(resp.text)
        data = state.get("data")
        entity = data.get("entity") if isinstance(data, dict) else None
        if not isinstance(entity, dict):
            raise SpotifyError("Spotify playlist is unavailable", 404)

        title_raw = entity.get("name") or entity.get("title") or "Spotify Playlist"
        title = str(title_raw)

        owner_raw = entity.get("subtitle")
        owner = owner_raw if isinstance(owner_raw, str) and owner_raw else None
        thumbnails = _cover_art(entity)

        metadata: dict[str, dict] = {}
        embed_ids: list[str] = []
        track_list = entity.get("trackList")
        items = track_list if isinstance(track_list, list) else []
        for item in items:
            track = _parse_embed_track(item)
            if track is None:
                continue
            metadata[track["id"]] = track
            embed_ids.append(track["id"])

        track_ids = (
            embed_ids
            if len(items) < 100
            else _fetch_full_track_ids(client, playlist_id, state)
        )

        missing = [track_id for track_id in track_ids if track_id not in metadata]
        if missing:
            if on_progress:
                on_progress("extracting", 0, len(missing))
            with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
                futures = [
                    pool.submit(_fetch_track, client, track_id)
                    for track_id in missing
                ]
                for completed, future in enumerate(as_completed(futures), start=1):
                    track = future.result()
                    if track is not None:
                        metadata[track["id"]] = track
                    if on_progress:
                        on_progress("extracting", completed, len(missing))

    tracks = [metadata[track_id] for track_id in track_ids if track_id in metadata]
    return {
        "title": title,
        "owner": owner,
        "thumbnails": thumbnails,
        "tracks": tracks,
        "total": len(track_ids),
    }


def _page_state(html: str) -> dict:
    match = _NEXT_DATA_RE.search(html)
    if match is None:
        raise SpotifyError("Could not parse Spotify page")

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise SpotifyError("Could not parse Spotify page") from exc

    state = data.get("props", {}).get("pageProps", {}).get("state")
    return state if isinstance(state, dict) else {}


def _fetch_full_track_ids(
    client: httpx.Client, playlist_id: str, state: dict
) -> list[str]:
    settings = state.get("settings") or {}
    session = settings.get("session") or {}
    token = session.get("accessToken")
    if not isinstance(token, str) or not token:
        raise SpotifyError("Could not fetch the complete Spotify playlist")

    resp = _get(
        client,
        f"https://spclient.wg.spotify.com/playlist/v2/playlist/{playlist_id}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
    )
    if resp.status_code != 200:
        raise SpotifyError(
            f"Spotify returned HTTP {resp.status_code} for this playlist"
        )

    try:
        payload = resp.json()
    except json.JSONDecodeError as exc:
        raise SpotifyError("Could not parse Spotify playlist contents") from exc

    contents = payload.get("contents") if isinstance(payload, dict) else None
    if not isinstance(contents, dict):
        raise SpotifyError("Could not parse Spotify playlist contents")

    if contents.get("truncated"):
        raise SpotifyError("Spotify playlist is too large to import")

    items = contents.get("items") or []
    track_ids = [
        track_id
        for item in items
        if isinstance(item, dict)
        and (track_id := _track_id(item.get("uri"))) is not None
    ]
    if not track_ids:
        raise SpotifyError("Spotify playlist has no tracks", 404)
    return track_ids


def _parse_embed_track(item: Any) -> dict | None:
    if not isinstance(item, dict):
        return None

    track_id = _track_id(item.get("uri"))
    title = item.get("title")
    if track_id is None or not isinstance(title, str) or not title:
        return None

    artist = item.get("subtitle")
    duration = item.get("duration")

    return {
        "id": track_id,
        "title": title,
        "artist": artist if isinstance(artist, str) and artist else "Unknown Artist",
        "duration": int(duration // 1000)
        if isinstance(duration, (int, float)) and duration > 0
        else 0,
    }


def _fetch_track(client: httpx.Client, track_id: str) -> dict | None:
    try:
        resp = _get(
            client, f"https://open.spotify.com/embed/track/{track_id}", timeout=10.0
        )
        if resp.status_code != 200:
            return None
        state = _page_state(resp.text)
    except SpotifyError:
        return None

    data = state.get("data")
    entity = data.get("entity") if isinstance(data, dict) else None
    if not isinstance(entity, dict):
        return None

    title = entity.get("name") or entity.get("title")
    if not isinstance(title, str) or not title:
        return None

    artists = entity.get("artists")
    names = (
        [
            artist["name"]
            for artist in artists
            if isinstance(artist, dict) and isinstance(artist.get("name"), str)
        ]
        if isinstance(artists, list)
        else []
    )
    duration = entity.get("duration")

    return {
        "id": track_id,
        "title": title,
        "artist": ", ".join(name for name in names if name) or "Unknown Artist",
        "duration": int(duration // 1000)
        if isinstance(duration, (int, float)) and duration > 0
        else 0,
    }


def _cover_art(entity: dict) -> list[dict]:
    cover = entity.get("coverArt")
    sources = cover.get("sources") if isinstance(cover, dict) else None
    if not isinstance(sources, list):
        return []
    return [
        {
            "url": source["url"],
            "width": source.get("width") or 0,
            "height": source.get("height") or 0,
        }
        for source in sources
        if isinstance(source, dict) and isinstance(source.get("url"), str)
    ]


def _track_id(uri: Any) -> str | None:
    if isinstance(uri, str) and uri.startswith(_TRACK_PREFIX):
        return uri[len(_TRACK_PREFIX) :] or None
    return None


def _extract_playlist_id(value: str) -> str:
    clean = value.split("?", 1)[0].split("#", 1)[0].strip()
    match = _PLAYLIST_ID_RE.search(clean)
    if match:
        return match.group(1)
    if _BARE_PLAYLIST_ID_RE.fullmatch(clean):
        return clean
    raise ValueError("Invalid Spotify playlist URL or ID")


# ── YouTube Music matching ──────────────────────────────────────────────────


def _match_tracks(
    config: Config,
    tracks: list[dict],
    on_progress: ProgressCallback | None = None,
) -> list[dict]:
    if not tracks:
        return []

    ordered: list[dict | None] = [None] * len(tracks)
    with make_ytm(config) as ytm, ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = {
            pool.submit(_match_track, ytm, track): index
            for index, track in enumerate(tracks)
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            ordered[futures[future]] = future.result()
            if on_progress:
                on_progress("matching", completed, len(tracks))

    return [entry for entry in ordered if entry is not None]


def _search(
    ytm: YTMusic,
    query: str,
    *,
    filter: str,
    limit: int,
) -> list[dict]:
    for attempt in (1, 2):
        try:
            return ytm.search(query=query, filter=filter, limit=limit)
        except Exception:
            # ytmusicapi surfaces throttled responses as assorted errors.
            if attempt == 2:
                raise
            time.sleep(0.5)
    return []


def _match_track(ytm: YTMusic, track: dict) -> dict | None:
    title = _clean_title(track["title"]) or track["title"]
    queries = [f"{track['artist']} - {title}"]
    if title != track["title"]:
        queries.append(f"{track['artist']} - {track['title']}")
    primary = _primary_artist(track["artist"])
    if primary != track["artist"]:
        queries.append(f"{primary} - {title}")

    best: dict | None = None
    best_score = -1.0
    try:
        for query in queries:
            candidates = _search(ytm, query, filter="songs", limit=5)
            candidate = _best_match(candidates, track)
            if candidate is None:
                continue
            score = _candidate_score(candidate, track)
            if score is not None and score > best_score:
                best = candidate
                best_score = score
            if (
                _title_score(track["title"], str(candidate.get("title") or ""))
                >= _NEAR_EXACT_TITLE_SCORE
                and _artist_overlap(track["artist"], candidate) > 0
            ):
                break
        if best is None:
            candidates = _search(ytm, queries[0], filter="videos", limit=3)
            best = _best_match(candidates, track)
    except Exception:
        # A single failed lookup must not abort the rest of the playlist.
        return None

    return parse_entry(best) if best is not None else None


def _primary_artist(spotify_artist: str) -> str:
    return spotify_artist.split(",", 1)[0].strip()


def _best_match(candidates: list[dict], track: dict) -> dict | None:
    best: dict | None = None
    best_score = -1.0
    for candidate in candidates:
        if not isinstance(candidate, dict) or not candidate.get("videoId"):
            continue
        score = _candidate_score(candidate, track)
        if score is None or score <= best_score:
            continue
        best = candidate
        best_score = score
    return best


def _candidate_score(candidate: dict, track: dict) -> float | None:
    title_score = _title_score(track["title"], str(candidate.get("title") or ""))
    if title_score < _MIN_TITLE_SCORE:
        return None

    overlap = _artist_overlap(track["artist"], candidate)
    artist_matched = overlap > 0
    if not artist_matched and title_score < _NEAR_EXACT_TITLE_SCORE:
        return None

    duration_score = 0.0
    if track["duration"] > 0:
        duration = _duration_seconds(candidate)
        if duration > 0:
            diff = abs(duration - track["duration"])
            duration_score = max(0.0, 1.0 - diff / _DURATION_HORIZON_SEC)

    return (
        (1.0 if artist_matched else 0.0) * _ARTIST_WEIGHT
        + title_score * _TITLE_WEIGHT
        + min(overlap, 4) * _ARTIST_OVERLAP_WEIGHT
        + duration_score * _DURATION_WEIGHT
    )


def _title_score(left: str, right: str) -> float:
    left_tokens = _canonical_title(left)
    right_tokens = _canonical_title(right)
    if not left_tokens or not right_tokens:
        return 0.0
    if left_tokens == right_tokens:
        return 1.0

    overlap = len(left_tokens & right_tokens)
    if overlap == 0:
        return 0.0

    precision = overlap / len(right_tokens)
    recall = overlap / len(left_tokens)
    return 2 * precision * recall / (precision + recall)


def _canonical_title(title: str) -> set[str]:
    text = _FEATURING_RE.sub(" ", title)
    qualifiers: list[str] = []

    def collect(match: re.Match) -> str:
        qualifiers.append(match.group(1))
        return " "

    base = re.sub(r"[\(\[]([^)\]]*)[\)\]]", collect, text)

    parts = base.split(" - ")
    if len(parts) > 1:
        for index in range(1, len(parts)):
            suffix = " - ".join(parts[index:])
            if any(word in _QUALIFIER_WORDS for word in _words(suffix)):
                base = " - ".join(parts[:index])
                qualifiers.append(suffix)
                break

    tokens = set(_words(base))
    for qualifier in qualifiers:
        words = _words(qualifier)
        if any(word in _QUALIFIER_WORDS for word in words):
            tokens.update(
                word
                for word in words
                if word not in _NOISE_WORDS and not _is_year(word)
            )
        else:
            tokens.update(words)
    return tokens


def _words(value: str) -> list[str]:
    normalized = unicodedata.normalize("NFKD", value)
    normalized = "".join(
        char for char in normalized if not unicodedata.combining(char)
    )
    return re.findall(r"[^\W_]+", normalized.lower(), re.UNICODE)


def _is_year(word: str) -> bool:
    return len(word) == 4 and word.isdigit()


def _artist_overlap(spotify_artist: str, candidate: dict) -> int:
    artists = candidate.get("artists")
    names = (
        [
            artist["name"]
            for artist in artists
            if isinstance(artist, dict) and isinstance(artist.get("name"), str)
        ]
        if isinstance(artists, list)
        else []
    )
    if not names:
        return 0

    target_names = {
        normalized
        for part in spotify_artist.split(",")
        if (normalized := _normalize_artist(part))
    }
    if not target_names:
        return 0

    if _normalize_artist(spotify_artist) == _normalize_artist(", ".join(names)):
        return len(target_names)

    matched: set[str] = set()
    for name in names:
        parts = [part for part in _ARTIST_SEPARATOR_RE.split(name) if part.strip()]
        components = {
            normalized
            for part in parts
            if (normalized := _normalize_artist(part))
        }
        matched.update(target_names & components)
        if len(parts) > 1:
            for target in target_names:
                if len(target) >= 4 and any(
                    component.startswith(target) for component in components
                ):
                    matched.add(target)
    return len(matched)


def _duration_seconds(candidate: dict) -> int:
    seconds = candidate.get("duration_seconds")
    if isinstance(seconds, (int, float)) and seconds > 0:
        return int(seconds)

    value = candidate.get("duration")
    if not isinstance(value, str):
        return 0

    parts = value.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except ValueError:
        pass
    return 0


def _clean_title(title: str) -> str:
    cleaned = _FEATURING_RE.sub("", title)
    cleaned = _VARIANT_RE.sub("", cleaned)
    cleaned = _VARIANT_SUFFIX_RE.sub("", cleaned)
    return cleaned.strip()


def _normalize_artist(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", name.lower())
