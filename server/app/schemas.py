"""Pydantic models for the own REST API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class TrackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    title: str
    artist: str
    album: str | None
    duration_seconds: float
    mime_type: str
    codec: str | None
    file_size: int
    bitrate_kbps: int | None
    source: Literal["upload", "youtube", "soundcloud"]
    source_url: str | None
    created_at: datetime
    play_count: int = 0
    last_played_at: datetime | None = None


class TrackPage(BaseModel):
    items: list[TrackOut]
    total: int
    limit: int
    offset: int


class TrackPatch(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=500)
    artist: str | None = Field(default=None, min_length=1, max_length=500)
    # Empty string clears the album.
    album: str | None = Field(default=None, max_length=500)

    @field_validator("title", "artist")
    @classmethod
    def _strip_required(cls, v: str | None) -> str | None:
        if v is not None and not v.strip():
            raise ValueError("must not be blank")
        return v.strip() if v else v


class StreamUrl(BaseModel):
    url: str = Field(description="Path + query, relative to the server base URL")
    expires_at: int


class ErrorOut(BaseModel):
    detail: str
    existing_track_id: uuid.UUID | None = None


class JobOut(BaseModel):
    id: uuid.UUID
    type: str
    kind: Literal["track", "playlist"] = "track"
    parent_id: uuid.UUID | None = None
    # playlist imports: entries, child jobs, progress counters, target playlist
    total: int | None = None
    children: int | None = None
    done: int | None = None
    failed: int | None = None
    already: int | None = None
    playlist_id: uuid.UUID | None = None
    playlist_name: str | None = None
    status: Literal["queued", "running", "done", "failed"]
    progress: int
    url: str | None
    source: str | None
    # Filled in by the worker once yt-dlp has read the metadata.
    title: str | None = None
    artist: str | None = None
    error: str | None
    attempts: int
    track: TrackOut | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
