from __future__ import annotations

import asyncio
import re

import httpx

from ..state import Config
from .ytm import _parse_length, make_ytm

TIER_WORD = 3
TIER_LINE = 2
TIER_PLAIN = 1

_TIER_NAMES = {TIER_WORD: "word", TIER_LINE: "line", TIER_PLAIN: "plain"}

_MAX_RACE_WAIT = 2.5

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    )
}

_VARIANT_RE = re.compile(
    r"[\(\[][^\)\]]*(?:video|audio|visualizer|clip|remaster|lyrics?)[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
_FEAT_RE = re.compile(r"\bfeat\.?.*|\bft\.?.*", re.IGNORECASE)


class LyricsError(Exception):
    pass


async def fetch(
    config: Config, client: httpx.AsyncClient, video_id: str
) -> dict | None:
    title, artist, duration, lyrics_id = await asyncio.to_thread(
        _resolve_meta, config, video_id
    )
    if not title:
        raise LyricsError(f"Could not resolve track metadata for {video_id}")

    clean_title = _sanitize_title(title)

    tasks: dict[str, asyncio.Task] = {
        "unison": asyncio.create_task(_unison(client, video_id, clean_title, artist)),
        "betterlyrics": asyncio.create_task(
            _betterlyrics(client, clean_title, artist, duration)
        ),
        "lrclib": asyncio.create_task(_lrclib(client, clean_title, artist, duration)),
        "lrcred": asyncio.create_task(_lrcred(client, clean_title, artist, duration)),
    }
    if lyrics_id:
        tasks["ytmusic"] = asyncio.create_task(_ytmusic(config, lyrics_id))

    best = await _race(tasks)
    if best is not None:
        best["tier"] = _TIER_NAMES[best["tier"]]
    return best


# ── Providers ────────────────────────────────────────────────────────────────


async def _unison(
    client: httpx.AsyncClient, video_id: str, title: str, artist: str
) -> dict | None:
    result = await _unison_lookup(client, {"v": video_id}, "unison-id")
    if result is None:
        result = await _unison_lookup(
            client, {"song": title, "artist": artist}, "unison-meta"
        )
    return result


async def _unison_lookup(
    client: httpx.AsyncClient, params: dict, source: str
) -> dict | None:
    try:
        resp = await client.get(
            "https://unison.betterlyrics.org/lyrics",
            params=params,
            headers=_HEADERS,
            timeout=1.1,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if not data.get("success", True) or not data.get("lyrics"):
            return None
        sync_type = data.get("syncType", "plain")
        tier = (
            TIER_WORD
            if sync_type == "richsync"
            else TIER_LINE
            if sync_type == "linesync"
            else TIER_PLAIN
        )
        return {
            "source": source,
            "tier": tier,
            "format": data.get("format", "lrc"),
            "lyrics": data["lyrics"],
        }
    except Exception:
        return None


async def _betterlyrics(
    client: httpx.AsyncClient, title: str, artist: str, duration: float
) -> dict | None:
    if duration <= 0:
        return None
    try:
        resp = await client.get(
            "https://api.betterlyrics.org/getLyrics",
            params={"s": title, "a": artist, "d": int(duration)},
            headers=_HEADERS,
            timeout=1.1,
        )
        if resp.status_code != 200 or "<tt" not in resp.text:
            return None
        return {
            "source": "betterlyrics-api",
            "tier": TIER_WORD if "<span" in resp.text else TIER_LINE,
            "format": "ttml",
            "lyrics": resp.text,
        }
    except Exception:
        return None


async def _lrclib(
    client: httpx.AsyncClient, title: str, artist: str, duration: float
) -> dict | None:
    try:
        resp = await client.get(
            "https://lrclib.net/api/search",
            params={"track_name": title, "artist_name": artist},
            headers=_HEADERS,
            timeout=1.2,
        )
        if resp.status_code != 200:
            return None
        hits = resp.json()
        if not hits:
            return None

        candidates = hits
        if duration > 0:
            matching = [h for h in hits if abs(h.get("duration", 0) - duration) <= 8]
            candidates = matching or hits

        synced = [h for h in candidates if h.get("syncedLyrics")]
        pool = synced or candidates
        if duration > 0:
            best = min(pool, key=lambda h: abs(h.get("duration", 0) - duration))
        else:
            best = pool[0]

        if best.get("syncedLyrics"):
            return {
                "source": "lrclib",
                "tier": TIER_LINE,
                "format": "lrc",
                "lyrics": best["syncedLyrics"],
            }
        if best.get("plainLyrics"):
            return {
                "source": "lrclib",
                "tier": TIER_PLAIN,
                "format": "plain",
                "lyrics": best["plainLyrics"],
            }
        return None
    except Exception:
        return None


async def _lrcred(
    client: httpx.AsyncClient, title: str, artist: str, duration: float
) -> dict | None:
    try:
        resp = await client.get(
            "https://lrc.red/search.json",
            params={"q": f"{title} {artist}"},
            headers=_HEADERS,
            timeout=1.1,
        )
        if resp.status_code != 200:
            return None
        hits = resp.json().get("hits", [])
        if not hits:
            return None

        if duration > 0:
            best = min(hits, key=lambda h: abs(h.get("duration", 0) - duration))
            if abs(best.get("duration", 0) - duration) > 10:
                return None
        else:
            best = hits[0]

        isrc = best["isrc"]
        ttml = await client.get(
            f"https://lrc.red/s/{isrc}.ttml", headers=_HEADERS, timeout=0.8
        )
        if ttml.status_code == 200 and "<tt" in ttml.text:
            return {
                "source": "lrcred",
                "tier": TIER_WORD if "<span" in ttml.text else TIER_LINE,
                "format": "ttml",
                "lyrics": ttml.text,
            }
        lrc = await client.get(
            f"https://lrc.red/s/{isrc}.lrc", headers=_HEADERS, timeout=0.8
        )
        if lrc.status_code == 200:
            return {
                "source": "lrcred",
                "tier": TIER_LINE,
                "format": "lrc",
                "lyrics": lrc.text,
            }
        return None
    except Exception:
        return None


async def _ytmusic(config: Config, lyrics_id: str) -> dict | None:
    return await asyncio.to_thread(_ytmusic_sync, config, lyrics_id)


def _ytmusic_sync(config: Config, lyrics_id: str) -> dict | None:
    with make_ytm(config) as ytm:
        # The timed attempt goes through the mobile client; retry the
        # desktop-style plain fetch before giving up on this provider.
        try:
            data = ytm.get_lyrics(browseId=lyrics_id, timestamps=True)
        except Exception:
            try:
                data = ytm.get_lyrics(browseId=lyrics_id)
            except Exception:
                return None
    if not data:
        return None

    if data.get("hasTimestamps"):
        lines = []
        for item in data.get("lyrics", []):
            minutes = item.start_time // 60000
            seconds = item.start_time % 60000 / 1000
            lines.append(f"[{minutes:02d}:{seconds:05.2f}]{item.text}")
        return {
            "source": "ytmusic-native",
            "tier": TIER_LINE,
            "format": "lrc",
            "lyrics": "\n".join(lines),
        }
    if data.get("lyrics"):
        return {
            "source": "ytmusic-native",
            "tier": TIER_PLAIN,
            "format": "plain",
            "lyrics": data["lyrics"],
        }
    return None


# ── Race ─────────────────────────────────────────────────────────────────────


async def _race(tasks: dict[str, asyncio.Task]) -> dict | None:
    """Runs provider tasks against a shared deadline, keeping the best tier.

    A word-level hit wins immediately; otherwise the highest tier seen by the
    deadline is returned. Unfinished tasks are cancelled."""
    pending: set[asyncio.Task] = set(tasks.values())
    deadline = asyncio.get_running_loop().time() + _MAX_RACE_WAIT
    best: dict | None = None

    while pending:
        time_left = deadline - asyncio.get_running_loop().time()
        if time_left <= 0:
            break

        done, pending = await asyncio.wait(
            pending, timeout=time_left, return_when=asyncio.FIRST_COMPLETED
        )
        for task in done:
            result = task.result()
            if result is not None and (best is None or result["tier"] > best["tier"]):
                best = result
        if best is not None and best["tier"] == TIER_WORD:
            break

    for task in pending:
        task.cancel()

    return best


# ── Metadata ─────────────────────────────────────────────────────────────────


def _resolve_meta(config: Config, video_id: str) -> tuple[str, str, float, str | None]:
    with make_ytm(config) as ytm:
        title = ""
        artist = ""
        duration = 0.0
        lyrics_id = None

        try:
            watch = ytm.get_watch_playlist(videoId=video_id)
            track = (watch.get("tracks") or [{}])[0]
            title = track.get("title") or ""
            artists = track.get("artists") or []
            artist = artists[0]["name"] if artists else ""
            duration = _parse_length(track.get("length")) or 0
            lyrics_id = watch.get("lyrics")
        except Exception:
            pass

        if not duration:
            try:
                song = ytm.get_song(video_id)
                duration = float(song.get("videoDetails", {}).get("lengthSeconds") or 0)
            except Exception:
                pass

    return title, artist, duration, lyrics_id


def _sanitize_title(title: str) -> str:
    cleaned = _VARIANT_RE.sub("", title)
    cleaned = _FEAT_RE.sub("", cleaned)
    return cleaned.strip()
