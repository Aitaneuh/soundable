from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import Select, func, select

from app.audio import UPLOAD_EXTENSIONS, NotAudioError, probe
from app.config import get_settings
from app.deps import SessionDep, TokenDep, bearer_scheme, require_token
from app.library import DuplicateTrackError, TrackMeta, ingest_file, track_path
from app.metadata import guess_from_filename
from app.models import Track
from app.schemas import ErrorOut, StreamUrl, TrackOut, TrackPage, TrackPatch
from app.security import sign_stream, verify_stream_signature
from app.uploads import BadUpload, UploadTooLarge, receive_upload

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/tracks", tags=["tracks"])

SortKey = Literal["recent", "artist", "title"]


def search_filter(stmt: Select, q: str | None) -> Select:  # type: ignore[type-arg]
    """Every word must appear (accent/case-insensitive) in title, artist or album."""
    if not q:
        return stmt
    haystack = func.unaccent(func.concat_ws(" ", Track.title, Track.artist, Track.album))
    for word in q.split()[:10]:
        escaped = word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = stmt.where(haystack.ilike(func.unaccent(f"%{escaped}%"), escape="\\"))
    return stmt


def _order(sort: SortKey) -> list:  # type: ignore[type-arg]
    if sort == "artist":
        return [func.lower(Track.artist), func.lower(Track.album), func.lower(Track.title)]
    if sort == "title":
        return [func.lower(Track.title), func.lower(Track.artist)]
    return [Track.created_at.desc(), Track.id]


async def get_track_or_404(session: SessionDep, track_id: uuid.UUID) -> Track:
    track = await session.get(Track, track_id)
    if track is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Track not found")
    return track


@router.get("")
async def list_tracks(
    session: SessionDep,
    _: TokenDep,
    q: Annotated[str | None, Query(max_length=200)] = None,
    sort: SortKey = "recent",
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> TrackPage:
    base = search_filter(select(Track), q)
    total = await session.scalar(select(func.count()).select_from(base.subquery()))
    rows = await session.scalars(base.order_by(*_order(sort)).limit(limit).offset(offset))
    return TrackPage(
        items=[TrackOut.model_validate(t) for t in rows],
        total=total or 0,
        limit=limit,
        offset=offset,
    )


@router.post(
    "/upload",
    status_code=status.HTTP_201_CREATED,
    responses={409: {"model": ErrorOut}, 413: {"model": ErrorOut}, 415: {"model": ErrorOut}},
)
async def upload_track(request: Request, session: SessionDep, _: TokenDep) -> TrackOut:
    """multipart/form-data with a `file` field and optional `title`/`artist`/`album`."""
    max_bytes = get_settings().max_upload_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes + 64 * 1024:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE, f"File too large (max {max_bytes // 2**20} MB)"
        )
    try:
        up = await receive_upload(request, max_bytes)
    except UploadTooLarge:
        raise HTTPException(
            status.HTTP_413_CONTENT_TOO_LARGE, f"File too large (max {max_bytes // 2**20} MB)"
        ) from None
    except BadUpload as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None

    ext = Path(up.filename).suffix.lower()
    try:
        if ext not in UPLOAD_EXTENSIONS:
            raise NotAudioError(
                f"unsupported file type '{ext or '?'}' "
                f"(allowed: {', '.join(sorted(UPLOAD_EXTENSIONS))})"
            )
        info = await asyncio.to_thread(probe, up.path)
    except NotAudioError as e:
        up.path.unlink(missing_ok=True)
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(e)) from None

    guessed_artist, guessed_title = guess_from_filename(Path(up.filename).stem)
    meta = TrackMeta(
        title=up.fields.get("title") or info.tags.get("title") or guessed_title,
        artist=up.fields.get("artist")
        or info.tags.get("artist")
        or guessed_artist
        or "Unknown artist",
        album=up.fields.get("album") or info.tags.get("album"),
    )
    try:
        track = await ingest_file(
            session, up.path, meta=meta, source="upload", sha256=up.sha256, info=info
        )
    except DuplicateTrackError as e:
        return JSONResponse(  # type: ignore[return-value]
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(e), "existing_track_id": str(e.existing.id)},
        )
    except NotAudioError as e:
        raise HTTPException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, str(e)) from None
    log.info("uploaded %s (%s - %s)", track.id, track.artist, track.title)
    return TrackOut.model_validate(track)


@router.get("/{track_id}")
async def get_track(track_id: uuid.UUID, session: SessionDep, _: TokenDep) -> TrackOut:
    return TrackOut.model_validate(await get_track_or_404(session, track_id))


@router.patch("/{track_id}")
async def edit_track(
    track_id: uuid.UUID, body: TrackPatch, session: SessionDep, _: TokenDep
) -> TrackOut:
    track = await get_track_or_404(session, track_id)
    data = body.model_dump(exclude_unset=True)
    if "title" in data and data["title"] is not None:
        track.title = data["title"]
    if "artist" in data and data["artist"] is not None:
        track.artist = data["artist"]
    if "album" in data:
        track.album = (data["album"] or "").strip() or None
    await session.commit()
    await session.refresh(track)
    return TrackOut.model_validate(track)


@router.delete("/{track_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_track(track_id: uuid.UUID, session: SessionDep, _: TokenDep) -> None:
    track = await get_track_or_404(session, track_id)
    path = track_path(track)
    await session.delete(track)
    await session.commit()
    # Delete the file only after the row is gone: a dangling file is harmless, a row
    # pointing at a missing file is not.
    path.unlink(missing_ok=True)
    log.info("deleted track %s", track_id)


@router.get("/{track_id}/stream-url")
async def stream_url(track_id: uuid.UUID, session: SessionDep, _: TokenDep) -> StreamUrl:
    """A signed URL that can be handed to mpv (or curl) without an Authorization header."""
    await get_track_or_404(session, track_id)
    exp, sig = sign_stream(str(track_id))
    return StreamUrl(url=f"/api/tracks/{track_id}/stream?exp={exp}&sig={sig}", expires_at=exp)


@router.get("/{track_id}/stream")
async def stream_track(
    track_id: uuid.UUID,
    session: SessionDep,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    exp: int | None = None,
    sig: str | None = None,
) -> FileResponse:
    """Serves the file with HTTP Range support (206 Partial Content) so players can seek.
    Auth: either a bearer token or a valid `exp`+`sig` from /stream-url."""
    if exp is not None and sig is not None:
        if not verify_stream_signature(str(track_id), exp, sig):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid or expired stream link")
    else:
        await require_token(session, creds)
    track = await get_track_or_404(session, track_id)
    return file_response(track)


def file_response(track: Track, *, download_name: str | None = None) -> FileResponse:
    path = track_path(track)
    if not path.is_file():
        log.error("file missing on disk for track %s: %s", track.id, path)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Audio file missing on server")
    return FileResponse(
        path,
        media_type=track.mime_type,
        filename=download_name,
        content_disposition_type="attachment" if download_name else "inline",
    )
