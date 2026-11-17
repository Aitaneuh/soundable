"""On-the-fly MP3 transcoding with ffmpeg, streamed to the client (for Subsonic clients
on mobile data: `stream?maxBitRate=128` or `format=mp3`).

Transcoded streams have no Content-Length and no Range support; clients seek in them
with `timeOffset` (OpenSubsonic "transcodeOffset" extension), which maps to ffmpeg -ss.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi.responses import StreamingResponse

log = logging.getLogger(__name__)

MP3_BITRATES = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)
DEFAULT_MP3_BITRATE = 256
_slots = asyncio.Semaphore(4)  # cap concurrent ffmpeg processes


def pick_bitrate(max_kbps: int | None) -> int:
    """Highest standard MP3 bitrate not above the client's limit."""
    if not max_kbps:
        return DEFAULT_MP3_BITRATE
    return max((b for b in MP3_BITRATES if b <= max_kbps), default=MP3_BITRATES[0])


def should_transcode(
    *, fmt: str | None, max_kbps: int | None, source_kbps: int | None, source_suffix: str
) -> bool:
    fmt = (fmt or "").lower()
    if fmt == "raw":
        return False
    over_limit = bool(max_kbps) and (source_kbps or 10_000) > max_kbps  # type: ignore[operator]
    if fmt == "mp3":
        return source_suffix != "mp3" or over_limit
    return over_limit


async def mp3_stream(path: Path, bitrate: int, offset: float = 0.0) -> StreamingResponse:
    await _slots.acquire()
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            *(["-ss", f"{offset:.3f}"] if offset > 0 else []),
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-vn",
            "-map_metadata",
            "-1",
            "-c:a",
            "libmp3lame",
            "-b:a",
            f"{bitrate}k",
            "-f",
            "mp3",
            "pipe:1",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
    except BaseException:
        _slots.release()
        raise

    async def body() -> AsyncIterator[bytes]:
        assert proc.stdout is not None
        try:
            while chunk := await proc.stdout.read(64 * 1024):
                yield chunk
        finally:
            # Client went away (or we're done): don't leave ffmpeg running.
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            await proc.wait()
            _slots.release()

    return StreamingResponse(
        body(),
        media_type="audio/mpeg",
        headers={"Cache-Control": "no-store", "Accept-Ranges": "none"},
    )
