from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from app import playlists as svc
from app.deps import SessionDep, TokenDep
from app.models import Playlist
from app.schemas import TrackOut

router = APIRouter(prefix="/api/playlists", tags=["playlists"])


class PlaylistOut(BaseModel):
    id: uuid.UUID
    name: str
    comment: str | None
    track_count: int
    duration_seconds: float
    created_at: datetime
    updated_at: datetime


class PlaylistDetail(PlaylistOut):
    tracks: list[TrackOut]


class PlaylistCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    comment: str | None = Field(default=None, max_length=1000)
    track_ids: list[uuid.UUID] = Field(default_factory=list, max_length=svc.MAX_TRACKS)


class PlaylistPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    comment: str | None = Field(default=None, max_length=1000)


class TracksIn(BaseModel):
    track_ids: list[uuid.UUID] = Field(max_length=svc.MAX_TRACKS)
    position: int | None = Field(default=None, ge=0, description="Insert here (default: end)")


class TrackOrder(BaseModel):
    track_ids: list[uuid.UUID] = Field(max_length=svc.MAX_TRACKS)


class MoveIn(BaseModel):
    from_position: int = Field(ge=0)
    to_position: int = Field(ge=0)


def detail(p: Playlist) -> PlaylistDetail:
    tracks = [TrackOut.model_validate(e.track) for e in p.entries]
    return PlaylistDetail(
        id=p.id,
        name=p.name,
        comment=p.comment,
        track_count=len(tracks),
        duration_seconds=sum(t.duration_seconds for t in tracks),
        created_at=p.created_at,
        updated_at=p.updated_at,
        tracks=tracks,
    )


@asynccontextmanager
async def _errors() -> AsyncIterator[None]:
    try:
        yield
    except svc.PlaylistNotFound as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from None
    except svc.DuplicateName as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from None
    except svc.PlaylistError as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None


@router.get("")
async def list_playlists(session: SessionDep, _: TokenDep) -> list[PlaylistOut]:
    return [
        PlaylistOut(
            id=s.playlist.id,
            name=s.playlist.name,
            comment=s.playlist.comment,
            track_count=s.track_count,
            duration_seconds=s.duration_seconds,
            created_at=s.playlist.created_at,
            updated_at=s.playlist.updated_at,
        )
        for s in await svc.list_playlists(session)
    ]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_playlist(body: PlaylistCreate, session: SessionDep, _: TokenDep) -> PlaylistDetail:
    async with _errors():
        return detail(await svc.create_playlist(session, body.name, body.track_ids, body.comment))


@router.get("/{playlist_id}")
async def get_playlist(playlist_id: uuid.UUID, session: SessionDep, _: TokenDep) -> PlaylistDetail:
    async with _errors():
        return detail(await svc.get_playlist(session, playlist_id))


@router.patch("/{playlist_id}")
async def update_playlist(
    playlist_id: uuid.UUID, body: PlaylistPatch, session: SessionDep, _: TokenDep
) -> PlaylistDetail:
    async with _errors():
        p = await svc.update_playlist(
            session,
            playlist_id,
            name=body.name,
            comment=body.comment,
            set_comment="comment" in body.model_fields_set,
        )
        return detail(p)


@router.delete("/{playlist_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_playlist(playlist_id: uuid.UUID, session: SessionDep, _: TokenDep) -> None:
    async with _errors():
        await svc.delete_playlist(session, playlist_id)


@router.post("/{playlist_id}/tracks")
async def add_tracks(
    playlist_id: uuid.UUID, body: TracksIn, session: SessionDep, _: TokenDep
) -> PlaylistDetail:
    """Append (or insert at `position`) tracks; duplicates are allowed."""
    async with _errors():
        return detail(await svc.add_tracks(session, playlist_id, body.track_ids, body.position))


@router.put("/{playlist_id}/tracks")
async def replace_tracks(
    playlist_id: uuid.UUID, body: TrackOrder, session: SessionDep, _: TokenDep
) -> PlaylistDetail:
    """Replace the whole track list (use it to reorder freely)."""
    async with _errors():
        return detail(await svc.set_tracks(session, playlist_id, body.track_ids))


@router.delete("/{playlist_id}/tracks")
async def remove_tracks(
    playlist_id: uuid.UUID,
    session: SessionDep,
    _: TokenDep,
    positions: Annotated[list[int], Query(min_length=1)],
) -> PlaylistDetail:
    """Remove entries by 0-based position: ?positions=0&positions=3"""
    async with _errors():
        return detail(await svc.remove_positions(session, playlist_id, positions))


@router.post("/{playlist_id}/move")
async def move_track(
    playlist_id: uuid.UUID, body: MoveIn, session: SessionDep, _: TokenDep
) -> PlaylistDetail:
    async with _errors():
        return detail(
            await svc.move_track(session, playlist_id, body.from_position, body.to_position)
        )
