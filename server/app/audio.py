"""Audio probing (mutagen) and conversion (ffmpeg).

iOS (AVFoundation, used by most Subsonic apps) reliably plays MP3, AAC/ALAC in MP4,
FLAC and WAV. Anything else (Opus, Vorbis, WebM...) is converted once to AAC in an .m4a
container at 256 kbps: transparent for practical purposes, and a lossy->lossy hop at a
high bitrate is the least bad option when the source is already lossy.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

import mutagen

log = logging.getLogger(__name__)

AAC_BITRATE = "256k"

# Extensions accepted for upload (checked before probing; the probe is what counts).
UPLOAD_EXTENSIONS = frozenset(
    {".mp3", ".m4a", ".mp4", ".aac", ".flac", ".wav", ".ogg", ".oga", ".opus", ".webm"}
)


class NotAudioError(ValueError):
    pass


@dataclass(frozen=True)
class AudioInfo:
    kind: str  # mutagen class name: MP3, MP4, FLAC, WAVE, AAC, OggOpus, ...
    codec: str  # mp3, aac, alac, flac, pcm, opus, vorbis, ...
    duration: float
    bitrate_kbps: int | None
    tags: dict[str, str]

    @property
    def ios_friendly(self) -> bool:
        return (self.kind, self.codec) in {
            ("MP3", "mp3"),
            ("MP4", "aac"),
            ("MP4", "alac"),
            ("FLAC", "flac"),
            ("WAVE", "pcm"),
            ("AAC", "aac"),
        }

    @property
    def extension(self) -> str:
        return {"MP3": "mp3", "MP4": "m4a", "FLAC": "flac", "WAVE": "wav", "AAC": "aac"}[self.kind]

    @property
    def mime_type(self) -> str:
        return {
            "MP3": "audio/mpeg",
            "MP4": "audio/mp4",
            "FLAC": "audio/flac",
            "WAVE": "audio/wav",
            "AAC": "audio/aac",
        }[self.kind]


def _codec(kind: str, info: object) -> str:
    if kind == "MP4":
        raw = str(getattr(info, "codec", "")).lower()
        if raw.startswith("mp4a"):
            return "aac"
        return raw or "unknown"
    return {
        "MP3": "mp3",
        "FLAC": "flac",
        "WAVE": "pcm",
        "AAC": "aac",
        "OggOpus": "opus",
        "OggVorbis": "vorbis",
        "OggFLAC": "flac",
    }.get(kind, kind.lower())


def _first_tag(f: mutagen.FileType, key: str) -> str | None:
    try:
        tags = f.tags
        if not tags:
            return None
        v = tags.get(key)
    except Exception:
        return None
    if not v:
        return None
    value = v[0] if isinstance(v, list) else v
    text = str(value).strip()
    return text or None


def probe(path: Path) -> AudioInfo:
    """Raise NotAudioError unless mutagen recognises the file as audio with a duration."""
    try:
        f = mutagen.File(path, easy=True)
    except Exception as e:  # mutagen raises many different error types on garbage
        raise NotAudioError(f"not a readable audio file ({e.__class__.__name__})") from e
    if f is None or f.info is None:
        raise NotAudioError("not a recognised audio file")
    duration = float(getattr(f.info, "length", 0) or 0)
    if duration <= 0:
        raise NotAudioError("audio file has no duration")
    kind = type(f).__name__.removeprefix("Easy")
    codec = _codec(kind, f.info)
    if kind == "MP4" and not getattr(f.info, "codec", None):
        raise NotAudioError("MP4 file without an audio track")
    bitrate = getattr(f.info, "bitrate", 0) or 0
    if not bitrate:
        bitrate = int(path.stat().st_size * 8 / duration)
    tags = {k: v for k in ("title", "artist", "album") if (v := _first_tag(f, k))}
    return AudioInfo(
        kind=kind,
        codec=codec,
        duration=duration,
        bitrate_kbps=round(bitrate / 1000) or None,
        tags=tags,
    )


async def convert_to_aac(src: Path, dst: Path) -> None:
    """Transcode src to AAC in an MP4 (.m4a) container. dst must end in .m4a."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(src),
        "-vn",
        "-map_metadata",
        "-1",
        "-c:a",
        "aac",
        "-b:a",
        AAC_BITRATE,
        "-movflags",
        "+faststart",
        str(dst),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, err = await proc.communicate()
    if proc.returncode != 0:
        dst.unlink(missing_ok=True)
        msg = err.decode(errors="replace").strip().splitlines()[-1:] or ["unknown error"]
        raise NotAudioError(f"conversion failed: {msg[0]}")
