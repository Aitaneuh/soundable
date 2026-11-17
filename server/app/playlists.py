"""Playlist operations, shared by the REST API and the Subsonic API.

Entries have contiguous 0-based positions. Every mutation rewrites the playlist's entry
list (delete + insert in one transaction): playlists are small, and this avoids juggling
the (playlist_id, position) primary key during reorders.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete, func, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import Playlist, PlaylistTrack, Track

MAX_TRACKS = 5000


class PlaylistError(ValueError):
    pass


class PlaylistNotFound(PlaylistError):
    pass


class DuplicateName(PlaylistError):
    pass


@dataclass
class PlaylistSummary:
    playlist: Playlist
    track_count: int
    duration_seconds: float


async def list_playlists(session: AsyncSession) -> list[PlaylistSummary]:
    stats = (
        select(
            PlaylistTrack.playlist_id,
            func.count().label("n"),
            func.coalesce(func.sum(Track.duration_seconds), 0).label("d"),
        )
        .join(Track, Track.id == PlaylistTrack.track_id)
        .group_by(PlaylistTrack.playlist_id)
        .subquery()
    )
    rows = await session.execute(
        select(Playlist, stats.c.n, stats.c.d)
        .outerjoin(stats, stats.c.playlist_id == Playlist.id)
        .order_by(func.lower(Playlist.name))
    )
    return [PlaylistSummary(p, n or 0, float(d or 0)) for p, n, d in rows]


async def get_playlist(session: AsyncSession, playlist_id: uuid.UUID) -> Playlist:
    p = await session.scalar(
        select(Playlist)
        .options(selectinload(Playlist.entries).joinedload(PlaylistTrack.track))
        .where(Playlist.id == playlist_id)
        .execution_options(populate_existing=True)
    )
    if p is None:
        raise PlaylistNotFound("Playlist not found")
    return p


def track_ids(p: Playlist) -> list[uuid.UUID]:
    return [e.track_id for e in p.entries]


async def _check_tracks(session: AsyncSession, ids: Sequence[uuid.UUID]) -> None:
    if len(ids) > MAX_TRACKS:
        raise PlaylistError(f"A playlist can hold at most {MAX_TRACKS} tracks")
    unique = set(ids)
    if not unique:
        return
    found = set(await session.scalars(select(Track.id).where(Track.id.in_(unique))))
    if missing := unique - found:
        raise PlaylistError(f"Unknown track id(s): {', '.join(sorted(map(str, missing)))}")


async def _write_entries(
    session: AsyncSession, playlist_id: uuid.UUID, ids: Sequence[uuid.UUID]
) -> None:
    await session.execute(delete(PlaylistTrack).where(PlaylistTrack.playlist_id == playlist_id))
    if ids:
        await session.execute(
            insert(PlaylistTrack),
            [{"playlist_id": playlist_id, "position": i, "track_id": t} for i, t in enumerate(ids)],
        )


async def _commit_name(session: AsyncSession, name: str) -> None:
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise DuplicateName(f"A playlist named '{name}' already exists") from None


def clean_name(name: str) -> str:
    name = " ".join(name.split())
    if not name:
        raise PlaylistError("The playlist name can't be empty")
    return name[:200]


async def create_playlist(
    session: AsyncSession,
    name: str,
    ids: Sequence[uuid.UUID] = (),
    comment: str | None = None,
) -> Playlist:
    name = clean_name(name)
    await _check_tracks(session, ids)
    p = Playlist(name=name, comment=comment)
    session.add(p)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        raise DuplicateName(f"A playlist named '{name}' already exists") from None
    await _write_entries(session, p.id, ids)
    await _commit_name(session, name)
    return await get_playlist(session, p.id)


async def update_playlist(
    session: AsyncSession,
    playlist_id: uuid.UUID,
    *,
    name: str | None = None,
    comment: str | None = None,
    set_comment: bool = False,
) -> Playlist:
    p = await get_playlist(session, playlist_id)
    if name is not None:
        p.name = clean_name(name)
    if set_comment:
        p.comment = comment or None
    p.updated_at = datetime.now(UTC)
    await _commit_name(session, p.name)
    return await get_playlist(session, playlist_id)


async def set_tracks(
    session: AsyncSession, playlist_id: uuid.UUID, ids: Sequence[uuid.UUID]
) -> Playlist:
    p = await get_playlist(session, playlist_id)
    await _check_tracks(session, ids)
    await _write_entries(session, p.id, ids)
    p.updated_at = datetime.now(UTC)
    await session.commit()
    return await get_playlist(session, playlist_id)


async def add_tracks(
    session: AsyncSession,
    playlist_id: uuid.UUID,
    ids: Sequence[uuid.UUID],
    position: int | None = None,
) -> Playlist:
    p = await get_playlist(session, playlist_id)
    current = track_ids(p)
    at = len(current) if position is None else max(0, min(position, len(current)))
    return await set_tracks(session, playlist_id, [*current[:at], *ids, *current[at:]])


async def remove_positions(
    session: AsyncSession, playlist_id: uuid.UUID, positions: Sequence[int]
) -> Playlist:
    p = await get_playlist(session, playlist_id)
    current = track_ids(p)
    bad = [i for i in positions if not 0 <= i < len(current)]
    if bad:
        raise PlaylistError(f"Position(s) out of range: {bad} (playlist has {len(current)})")
    drop = set(positions)
    return await set_tracks(
        session, playlist_id, [t for i, t in enumerate(current) if i not in drop]
    )


async def move_track(session: AsyncSession, playlist_id: uuid.UUID, src: int, dst: int) -> Playlist:
    p = await get_playlist(session, playlist_id)
    current = track_ids(p)
    if not (0 <= src < len(current) and 0 <= dst < len(current)):
        raise PlaylistError(f"Position out of range (playlist has {len(current)} tracks)")
    item = current.pop(src)
    current.insert(dst, item)
    return await set_tracks(session, playlist_id, current)


async def delete_playlist(session: AsyncSession, playlist_id: uuid.UUID) -> None:
    p = await session.get(Playlist, playlist_id)
    if p is None:
        raise PlaylistNotFound("Playlist not found")
    await session.delete(p)
    await session.commit()
