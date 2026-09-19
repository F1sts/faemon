"""Zero moov header durations in YouTube's fragmented MP4 audio.

YouTube DASH audio stores the full track duration in the moov header boxes
even though the media lives in sidx-indexed moof fragments. AVFoundation
(WebKit on macOS/Linux) sums both and reports 2x the real duration. CMAF
requires header durations of 0 in fragmented files; zeroing them in place
changes no box sizes, so Content-Length, Content-Range and sidx offsets
stay byte-valid. Chromium (WebView2 on Windows) reads these files
correctly, so the patcher is disabled there.
"""

from __future__ import annotations

import struct
import sys

_AUDIO_MP4_TYPES = ("mp4", "m4a", "aac")
_HEADER_SCAN_LIMIT = 256 * 1024
_PRE_MOOV_BOXES = (b"ftyp", b"free", b"skip", b"pdin", b"prft")


def _iter_boxes(data: bytearray, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size = struct.unpack_from(">I", data, pos)[0]
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size = struct.unpack_from(">Q", data, pos + 8)[0]
            header = 16
        if size < header or pos + size > end:
            return
        yield bytes(data[pos + 4 : pos + 8]), pos, header, size
        pos += size


def _zero_duration(d: bytearray, pos: int, header: int, v0_off: int, v1_off: int) -> None:
    version = d[pos + header]
    if version == 0:
        struct.pack_into(">I", d, pos + header + v0_off, 0)
    elif version == 1:
        struct.pack_into(">Q", d, pos + header + v1_off, 0)


def _patch_fragmented_moov(d: bytearray, start: int, end: int) -> None:
    if not any(t == b"mvex" for t, _, _, _ in _iter_boxes(d, start, end)):
        return
    for box, pos, header, size in _iter_boxes(d, start, end):
        if box == b"mvhd":
            _zero_duration(d, pos, header, 16, 28)
        elif box == b"trak":
            for child, cpos, cheader, csize in _iter_boxes(d, pos + header, pos + size):
                if child == b"tkhd":
                    _zero_duration(d, cpos, cheader, 20, 32)
                elif child == b"mdia":
                    for leaf, lpos, lheader, _ in _iter_boxes(
                        d, cpos + cheader, cpos + csize
                    ):
                        if leaf == b"mdhd":
                            _zero_duration(d, lpos, lheader, 16, 28)


def _header_complete(pending: bytearray) -> bool:
    """True once the buffered prefix is enough to patch (or rule out patching)."""
    if len(pending) > _HEADER_SCAN_LIMIT:
        return True
    pos = 0
    while True:
        if pos + 8 > len(pending):
            return False
        size = struct.unpack_from(">I", pending, pos)[0]
        header = 8
        if size == 1:
            if pos + 16 > len(pending):
                return False
            size = struct.unpack_from(">Q", pending, pos + 8)[0]
            header = 16
        if size < header or size == 0:
            return True
        if pos + size > len(pending):
            return False
        box = bytes(pending[pos + 4 : pos + 8])
        if box == b"moov":
            _patch_fragmented_moov(pending, pos + header, pos + size)
            return True
        if box not in _PRE_MOOV_BOXES:
            return True
        pos += size


class HeaderPatcher:
    """Buffers a stream prefix until the moov box is patchable, then passes
    everything through untouched."""

    def __init__(self) -> None:
        self._pending: bytearray | None = bytearray()

    def feed(self, chunk: bytes) -> bytes | None:
        if self._pending is None:
            return chunk
        self._pending.extend(chunk)
        if not _header_complete(self._pending):
            return None
        out = bytes(self._pending)
        self._pending = None
        return out

    def flush(self) -> bytes:
        if self._pending is None:
            return b""
        out = bytes(self._pending)
        self._pending = None
        return out


def patcher_for(content_type: str, content_range: str) -> HeaderPatcher | None:
    """Patcher for stream responses that include byte 0 of fragmented MP4
    audio; None when patching does not apply."""
    if sys.platform == "win32":
        return None
    if not any(t in content_type.lower() for t in _AUDIO_MP4_TYPES):
        return None
    if content_range and not content_range.startswith("bytes 0-"):
        return None
    return HeaderPatcher()
