from __future__ import annotations

import html
import json
import random
import re
import sys
import threading
import time
import unicodedata
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any
from urllib.parse import unquote_plus

import httpx
from ytmusicapi import YTMusic

from ..cache import MatchCache
from ..state import Config, State
from .ytm import make_ytm, parse_entry

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
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
_LOCAL_PREFIX = "spotify:local:"
_ARTIST_SEPARATOR_RE = re.compile(
    r"\s*(?:&|\+|\band\b|\bwith\b)\s*", re.IGNORECASE
)
_OG_TITLE_RE = re.compile(r'<meta property="og:title" content="(.*?)"')
_OG_DESCRIPTION_RE = re.compile(r'<meta property="og:description" content="(.*?)"')
_WORKERS = 8
_SPOTIFY_REQ_PER_SEC = 4.0
_YTM_REQ_PER_SEC = 4.0
_GET_ATTEMPTS = 6

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


class _Cancelled(BaseException):
    """Raised inside workers once the client has gone; escapes generic handlers."""


class _RateLimiter:
    """Paces Spotify requests process-wide; pause_all coordinates 429 backoff."""

    def __init__(self, rate_per_sec: float) -> None:
        self._interval = 1.0 / rate_per_sec
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            sleep_for = max(0.0, self._next_allowed - now)
            self._next_allowed = (
                max(now, self._next_allowed)
                + self._interval
                + random.uniform(0.02, 0.08)
            )
        if sleep_for > 0:
            time.sleep(sleep_for)

    def pause_all(self, seconds: float) -> None:
        with self._lock:
            # Failures landing inside an active cooldown don't extend it;
            # otherwise a burst of failing workers ratchets the debt into
            # minutes of total silence.
            now = time.monotonic()
            if now >= self._next_allowed:
                self._next_allowed = now + seconds


_SPOTIFY_LIMITER = _RateLimiter(_SPOTIFY_REQ_PER_SEC)
_YTM_LIMITER = _RateLimiter(_YTM_REQ_PER_SEC)


def playlist(
    state: State,
    url_or_id: str,
    on_progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> dict[str, Any]:
    playlist_id = _extract_playlist_id(url_or_id)
    if on_progress:
        on_progress("extracting", 0, 0)

    source = _fetch_playlist(playlist_id, on_progress, state.spotify_matches, cancel)
    if cancel is not None and cancel.is_set():
        raise _Cancelled()
    if on_progress:
        on_progress("matching", 0, len(source["tracks"]))

    entries, unmatched = _match_tracks(
        state.config, source["tracks"], on_progress, state.spotify_matches, cancel
    )

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
        "unmatched": unmatched,
    }


def match(state: State, tracks: list[Any]) -> dict[str, Any]:
    """Re-runs matching for tracks a previous import could not resolve."""
    clean = [
        {
            "id": str(track.get("id") or f"retry:{index}"),
            "title": str(track.get("title") or ""),
            "artist": str(track.get("artist") or "Unknown Artist"),
            "duration": int(track.get("duration") or 0),
        }
        for index, track in enumerate(tracks)
        if isinstance(track, dict) and track.get("title")
    ]
    if not clean:
        raise ValueError("No tracks with a title to match")

    entries, unmatched = _match_tracks(
        state.config, clean, None, state.spotify_matches
    )
    return {
        "entries": entries,
        "matched_count": len(entries),
        "unmatched": unmatched,
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
    for attempt in range(_GET_ATTEMPTS):
        _SPOTIFY_LIMITER.wait()
        try:
            response = client.get(url, headers=headers, timeout=timeout)
        except httpx.HTTPError as exc:
            if attempt == _GET_ATTEMPTS - 1:
                raise SpotifyError(f"Could not reach Spotify: {exc}") from exc
            time.sleep(_backoff(attempt))
            continue

        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            wait_sec = (
                float(retry_after)
                if retry_after is not None and retry_after.isdigit()
                else min(30.0, 4.0 * (attempt + 1))
            )
            _SPOTIFY_LIMITER.pause_all(wait_sec)
            continue

        if response.status_code >= 500 and attempt < _GET_ATTEMPTS - 1:
            time.sleep(_backoff(attempt))
            continue

        return response

    if response is None:
        raise SpotifyError("Could not reach Spotify")
    return response


def _backoff(attempt: int) -> float:
    return 1.5**attempt + random.uniform(0.2, 0.6)


def _fetch_playlist(
    playlist_id: str,
    on_progress: ProgressCallback | None = None,
    cache: MatchCache | None = None,
    cancel: threading.Event | None = None,
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

        # The embed payload caps and omits episodes/locals unpredictably (seen
        # returning as few as 99 items for a 900+ track playlist), so the
        # spclient listing is always the source of truth when a token exists.
        track_ids = embed_ids
        local_tracks: dict[str, dict] = {}
        session = (state.get("settings") or {}).get("session") or {}
        token = session.get("accessToken")
        if isinstance(token, str) and token:
            try:
                track_ids, local_tracks = _fetch_full_track_ids(
                    client, playlist_id, token
                )
            except SpotifyError as exc:
                print(
                    f"[faemon] spotify: full tracklist unavailable ({exc}); "
                    f"falling back to the first {len(embed_ids)} embed tracks",
                    file=sys.stderr,
                    flush=True,
                )
        elif len(items) >= 100:
            print(
                f"[faemon] spotify: no embed session token; only the first "
                f"{len(embed_ids)} of a larger playlist will be imported",
                file=sys.stderr,
                flush=True,
            )

        unresolved = [
            track_id
            for track_id in track_ids
            if track_id not in metadata and track_id not in local_tracks
        ]
        cached_ids: set[str] = set()
        if cache is not None:
            cached_ids = {
                track_id
                for track_id in unresolved
                if cache.get(track_id) is not None
            }
            for track_id in cached_ids:
                # Title stays empty: matching resolves these from the cache,
                # and an eviction mid-run degrades to unmatched, not garbage.
                metadata[track_id] = {
                    "id": track_id,
                    "title": "",
                    "artist": "",
                    "duration": 0,
                }
        missing = [
            track_id for track_id in unresolved if track_id not in cached_ids
        ]
        if missing:
            # Progress reads against the full tracklist; tracks whose metadata
            # came free with the embed page (and cache hits) count as done.
            if on_progress:
                on_progress("extracting", len(track_ids) - len(missing), len(track_ids))
            with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
                futures = [
                    pool.submit(_fetch_track, client, track_id, cancel)
                    for track_id in missing
                ]
                for completed, future in enumerate(as_completed(futures), start=1):
                    track = future.result()
                    if track is not None:
                        metadata[track["id"]] = track
                    if on_progress:
                        on_progress(
                            "extracting",
                            len(track_ids) - len(missing) + completed,
                            len(track_ids),
                        )
                    if cancel is not None and cancel.is_set():
                        break

    tracks = [
        local_tracks.get(track_id) or metadata.get(track_id)
        for track_id in track_ids
    ]
    return {
        "title": title,
        "owner": owner,
        "thumbnails": thumbnails,
        "tracks": [track for track in tracks if track is not None],
        "total": len(track_ids),
    }


def _page_props(page: str) -> dict:
    match = _NEXT_DATA_RE.search(page)
    if match is None:
        raise SpotifyError("Could not parse Spotify page")

    try:
        data = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise SpotifyError("Could not parse Spotify page") from exc

    props = data.get("props", {}).get("pageProps")
    return props if isinstance(props, dict) else {}


def _page_state(page: str) -> dict:
    state = _page_props(page).get("state")
    return state if isinstance(state, dict) else {}


def _fetch_full_track_ids(
    client: httpx.Client, playlist_id: str, token: str
) -> tuple[list[str], dict[str, dict]]:
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

    track_ids: list[str] = []
    local_tracks: dict[str, dict] = {}
    for item in contents.get("items") or []:
        if not isinstance(item, dict):
            continue
        uri = item.get("uri")
        if not isinstance(uri, str):
            continue
        if uri.startswith(_TRACK_PREFIX):
            track_id = uri[len(_TRACK_PREFIX) :]
            if track_id:
                track_ids.append(track_id)
        elif uri.startswith(_LOCAL_PREFIX):
            track = _parse_local_track(uri)
            if track is not None:
                local_tracks[track["id"]] = track
                track_ids.append(track["id"])
    if not track_ids:
        raise SpotifyError("Spotify playlist has no tracks", 404)
    return track_ids, local_tracks


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


def _fetch_track(
    client: httpx.Client,
    track_id: str,
    cancel: threading.Event | None = None,
) -> dict | None:
    if cancel is not None and cancel.is_set():
        return None
    try:
        resp = _get(
            client, f"https://open.spotify.com/embed/track/{track_id}", timeout=10.0
        )
        if resp.status_code != 200:
            return None
        props = _page_props(resp.text)
    except SpotifyError:
        return None

    state = props.get("state")
    data = state.get("data") if isinstance(state, dict) else None
    entity = data.get("entity") if isinstance(data, dict) else None
    if not isinstance(entity, dict):
        entity = props.get("entity")
    if isinstance(entity, dict):
        track = _track_from_entity(entity, track_id)
        if track is not None:
            return track

    return _track_from_meta_tags(resp.text, track_id)


def _track_from_entity(entity: dict, track_id: str) -> dict | None:
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
    fallback = entity.get("subtitle")
    artist = (
        ", ".join(name for name in names if name)
        or (fallback if isinstance(fallback, str) and fallback else "")
        or "Unknown Artist"
    )
    duration = entity.get("duration")

    return {
        "id": track_id,
        "title": title,
        "artist": artist,
        "duration": int(duration // 1000)
        if isinstance(duration, (int, float)) and duration > 0
        else 0,
    }


def _track_from_meta_tags(page: str, track_id: str) -> dict | None:
    """Recovers title/artist from OpenGraph tags when the embed payload is empty."""
    title_match = _OG_TITLE_RE.search(page)
    if title_match is None:
        return None
    title = html.unescape(title_match.group(1)).strip()
    if not title:
        return None

    artist = "Unknown Artist"
    desc_match = _OG_DESCRIPTION_RE.search(page)
    if desc_match is not None:
        first = html.unescape(desc_match.group(1)).split("·")[0].strip()
        if first:
            artist = first

    return {"id": track_id, "title": title, "artist": artist, "duration": 0}


def _parse_local_track(uri: str) -> dict | None:
    """Builds a searchable track from a spotify:local: file URI."""
    parts = uri.split(":")
    artist = unquote_plus(parts[2]).strip() if len(parts) > 2 else ""
    title = unquote_plus(parts[4]).strip() if len(parts) > 4 else ""
    duration_ms = int(parts[5]) if len(parts) > 5 and parts[5].isdigit() else 0
    if not title:
        return None

    return {
        "id": f"local:{artist}:{title}:{duration_ms}",
        "title": title,
        "artist": artist or "Unknown Artist",
        "duration": duration_ms // 1000,
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
    cache: MatchCache | None = None,
    cancel: threading.Event | None = None,
) -> list[dict]:
    if not tracks:
        return []

    positions: dict[str, list[int]] = {}
    unique: list[dict] = []
    for index, track in enumerate(tracks):
        positions.setdefault(track["id"], []).append(index)
        if len(positions[track["id"]]) == 1:
            unique.append(track)

    ordered: list[dict | None] = [None] * len(tracks)
    completed = 0

    def place(track_id: str, entry: dict | None, emit: bool = True) -> None:
        nonlocal completed
        if entry is not None:
            for index in positions[track_id]:
                ordered[index] = entry
        completed += len(positions[track_id])
        if on_progress and emit:
            on_progress("matching", completed, len(tracks))

    pending: list[dict] = []
    for track in unique:
        cached = cache.get(track["id"]) if cache is not None else None
        if cached is not None:
            place(track["id"], cached)
        else:
            pending.append(track)

    retry: list[dict] = []

    def handle(track: dict, entry: dict | None, retriable: bool) -> None:
        if entry is not None:
            if cache is not None:
                cache.set(track["id"], entry)
            place(track["id"], entry)
        elif retriable:
            retry.append(track)
        else:
            place(track["id"], None)

    if pending and not (cancel is not None and cancel.is_set()):
        _resolve_batch(config, pending, cancel, handle)

    if retry and not (cancel is not None and cancel.is_set()):
        # Time-separated from the failure: the throttle window that killed
        # the first pass has usually closed by the time the bulk finished.
        done = 0
        if on_progress:
            on_progress("retrying", 0, len(retry))

        def handle_retry(track: dict, entry: dict | None, retriable: bool) -> None:
            nonlocal done
            if entry is not None and cache is not None:
                cache.set(track["id"], entry)
            place(track["id"], entry, emit=False)
            done += 1
            if on_progress:
                on_progress("retrying", done, len(retry))

        _resolve_batch(config, retry, cancel, handle_retry)
        if on_progress:
            on_progress("matching", completed, len(tracks))

    unmatched: list[dict[str, str | int]] = []
    reported: set[str] = set()
    for track, entry in zip(tracks, ordered):
        if entry is not None or track["id"] in reported or not track["title"]:
            continue
        reported.add(track["id"])
        unmatched.append(
            {
                "id": track["id"],
                "title": track["title"],
                "artist": track["artist"],
                "duration": track["duration"],
            }
        )

    return [entry for entry in ordered if entry is not None], unmatched


def _resolve_batch(
    config: Config,
    tracks: list[dict],
    cancel: threading.Event | None = None,
    on_result: Callable[[dict, dict | None, bool], None] | None = None,
) -> None:
    with make_ytm(config) as ytm, ThreadPoolExecutor(max_workers=_WORKERS) as pool:
        futures = {
            pool.submit(_match_track, ytm, track, cancel): track
            for track in tracks
        }
        for future in as_completed(futures):
            track = futures[future]
            entry, retriable = future.result()
            if on_result is not None:
                on_result(track, entry, retriable)


def _search(
    ytm: YTMusic,
    query: str,
    *,
    filter: str,
    limit: int,
    cancel: threading.Event | None = None,
) -> list[dict]:
    for attempt in range(3):
        if cancel is not None and cancel.is_set():
            raise _Cancelled()
        _YTM_LIMITER.wait()
        try:
            return ytm.search(query=query, filter=filter, limit=limit)
        except Exception:
            # ytmusicapi surfaces throttled responses as assorted errors, and
            # a failure usually means the IP is throttled as a whole — cool
            # the shared limiter down so all workers back off together
            # instead of re-arming into the throttle.
            if attempt == 2:
                raise
            _YTM_LIMITER.pause_all(2.0 * (attempt + 1))
            time.sleep(1.0 * (attempt + 1))
    return []


def _match_track(
    ytm: YTMusic,
    track: dict,
    cancel: threading.Event | None = None,
) -> tuple[dict | None, bool]:
    """Returns (entry, retriable); retriable marks a transient lookup failure."""
    if not track["title"]:
        return None, False

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
            candidates = _search(ytm, query, filter="songs", limit=5, cancel=cancel)
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
            candidates = _search(
                ytm, queries[0], filter="videos", limit=3, cancel=cancel
            )
            best = _best_match(candidates, track)
    except _Cancelled:
        return None, False
    except Exception:
        # A transient failure must not abort the batch; the retry pass re-runs it.
        return None, True

    return (parse_entry(best) if best is not None else None), False


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
