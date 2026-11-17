"""Turning a downloaded/uploaded temp file into a library track (shared by the upload
endpoint and the import worker), and file housekeeping."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.audio import AudioInfo, convert_to_aac, probe
from app.config import get_settings
from app.models import Track

log = logging.getLogger(__name__)

TMP_DIRNAME = ".tmp"


class DuplicateTrackError(Exception):
    def __init__(self, existing: Track) -> None:
        super().__init__(f"already in library: {existing.artist} - {existing.title}")
        self.existing = existing


def music_dir() -> Path:
    return get_settings().music_dir


def tmp_dir() -> Path:
    d = music_dir() / TMP_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def new_tmp_path(suffix: str = "") -> Path:
    return tmp_dir() / f"{uuid.uuid4().hex}{suffix}"


def cleanup_stale_tmp(max_age_seconds: int = 24 * 3600) -> None:
    """Remove leftovers from crashed uploads/imports. Only old files, because the
    worker may be writing into the same directory right now."""
    cutoff = time.time() - max_age_seconds
    for p in tmp_dir().iterdir():
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
        except OSError:
            pass


def track_path(track: Track) -> Path:
    return music_dir() / track.file_path


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class TrackMeta:
    title: str
    artist: str
    album: str | None = None


async def find_duplicate(
    session: AsyncSession, *, sha256: str | None = None, source_key: str | None = None
) -> Track | None:
    if sha256:
        t = await session.scalar(select(Track).where(Track.content_sha256 == sha256))
        if t:
            return t
    if source_key:
        return await session.scalar(select(Track).where(Track.source_key == source_key))
    return None


async def ingest_file(
    session: AsyncSession,
    tmp: Path,
    *,
    meta: TrackMeta,
    source: str,
    sha256: str,
    info: AudioInfo | None = None,
    source_url: str | None = None,
    source_key: str | None = None,
) -> Track:
    """Probe/convert `tmp`, move it atomically into MUSIC_DIR and insert the track.

    `tmp` is consumed: on success it has been moved, on failure it is deleted.
    `sha256` is the hash of the *original* content (before any conversion) so the same
    upload is recognised as a duplicate the second time.
    """
    produced: list[Path] = [tmp]
    final: Path | None = None
    try:
        dup = await find_duplicate(session, sha256=sha256, source_key=source_key)
        if dup:
            raise DuplicateTrackError(dup)

        info = info or await asyncio.to_thread(probe, tmp)
        if not info.ios_friendly:
            converted = new_tmp_path(".m4a")
            produced.append(converted)
            log.info("converting %s/%s to AAC", info.kind, info.codec)
            await convert_to_aac(tmp, converted)
            tmp.unlink(missing_ok=True)
            tmp = converted
            info = await asyncio.to_thread(probe, tmp)

        track_id = uuid.uuid4()
        rel = f"{track_id}.{info.extension}"
        final = music_dir() / rel
        size = tmp.stat().st_size
        os.replace(tmp, final)  # atomic within the same filesystem (.tmp is inside)

        track = Track(
            id=track_id,
            title=meta.title.strip()[:500] or "Unknown title",
            artist=meta.artist.strip()[:500] or "Unknown artist",
            album=(meta.album or "").strip()[:500] or None,
            duration_seconds=info.duration,
            file_path=rel,
            mime_type=info.mime_type,
            codec=info.codec,
            file_size=size,
            bitrate_kbps=info.bitrate_kbps,
            source=source,
            source_url=source_url,
            source_key=source_key,
            content_sha256=sha256,
        )
        session.add(track)
        try:
            await session.commit()
        except IntegrityError:
            # Lost a race against an identical concurrent upload/import.
            await session.rollback()
            dup = await find_duplicate(session, sha256=sha256, source_key=source_key)
            if dup:
                raise DuplicateTrackError(dup) from None
            raise
        final = None  # committed: keep the file
        return track
    finally:
        for p in produced:
            p.unlink(missing_ok=True)
        if final is not None:
            final.unlink(missing_ok=True)
