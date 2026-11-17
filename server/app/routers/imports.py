from __future__ import annotations

import asyncio
import dataclasses
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from app import playlist_import
from app.deps import SessionDep, TokenDep
from app.importer import ImportFailed, inspect_link
from app.jobs import IMPORT_PLAYLIST, IMPORT_URL, job_out
from app.library import find_duplicate
from app.models import Job
from app.schemas import ErrorOut, JobOut
from app.sources import UnsupportedUrl, parse_import_url, resolve_soundcloud_short

router = APIRouter(prefix="/api", tags=["imports"])

JobStatus = Literal["queued", "running", "done", "failed"]


class ImportIn(BaseModel):
    url: str = Field(min_length=1, max_length=2000)
    # auto: whatever the link is; track: only the video of a video-in-playlist link;
    # playlist: the whole playlist (also for a video-in-playlist link).
    mode: Literal["auto", "track", "playlist"] = "auto"
    # For playlist imports: also create a soundable playlist with this name.
    playlist_name: str | None = Field(default=None, max_length=200)


class InspectIn(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


class InspectOut(BaseModel):
    kind: Literal["track", "playlist"]
    url: str
    source: str
    title: str | None = None
    count: int | None = None
    playlist_url: str | None = None
    playlist_title: str | None = None
    playlist_count: int | None = None


INSPECT_TIMEOUT = 60.0


@router.post("/imports/inspect", responses={400: {"model": ErrorOut}})
async def inspect_import(body: InspectIn, _: TokenDep) -> InspectOut:
    """What a link points to, before importing: a track, a playlist (title + number of
    tracks), or a video inside a playlist. Playlists are listed without downloading."""
    try:
        info = await asyncio.wait_for(
            asyncio.to_thread(inspect_link, body.url), timeout=INSPECT_TIMEOUT
        )
    except UnsupportedUrl as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None
    except ImportFailed as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, e.message) from None
    except (TimeoutError, OSError):
        raise HTTPException(
            status.HTTP_504_GATEWAY_TIMEOUT, "Timed out while reading the playlist"
        ) from None
    return InspectOut(**dataclasses.asdict(info))


async def _active_job(session: SessionDep, job_type: str, url: str) -> Job | None:
    return await session.scalar(
        select(Job)
        .options(selectinload(Job.result_track))
        .where(
            Job.type == job_type,
            Job.status.in_(("queued", "running")),
            Job.payload["url"].astext == url,
            ~Job.payload.has_key("parent_id"),
        )
        .limit(1)
    )


@router.post(
    "/imports",
    status_code=status.HTTP_201_CREATED,
    responses={200: {"model": JobOut}, 400: {"model": ErrorOut}, 409: {"model": ErrorOut}},
)
async def create_import(body: ImportIn, session: SessionDep, _: TokenDep) -> JobOut:
    """Queue a YouTube/SoundCloud import. Returns the job to poll with GET /api/jobs/{id}.

    - 409 if the track is already in the library (YouTube links are checked immediately,
      SoundCloud ones once the worker knows the track id).
    - 200 with the existing job if the same link is already queued or running.
    - Playlist imports create one job per track (hidden from GET /api/jobs by default);
      the parent job reports progress and, with `playlist_name`, fills that playlist.
    """
    try:
        target = parse_import_url(body.url)
        if target.needs_resolve and body.mode != "track":
            target = await asyncio.to_thread(resolve_soundcloud_short, target.url)
    except UnsupportedUrl as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None
    except OSError:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "Couldn't resolve the short link"
        ) from None

    as_playlist = target.kind == "playlist" or (body.mode == "playlist" and target.playlist_url)
    if body.mode == "playlist" and not as_playlist:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That link isn't a playlist")
    if body.mode == "track" and target.kind == "playlist":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "That link is a playlist, not a track")

    if as_playlist:
        url = target.url if target.kind == "playlist" else str(target.playlist_url)
        if active := await _active_job(session, IMPORT_PLAYLIST, url):
            return JSONResponse(  # type: ignore[return-value]
                status_code=status.HTTP_200_OK, content=job_out(active).model_dump(mode="json")
            )
        name = (body.playlist_name or "").strip() or None
        job = Job(
            type=IMPORT_PLAYLIST,
            payload={
                "input": body.url.strip(),
                "url": url,
                "source": target.source,
                "playlist_name": name,
            },
        )
    else:
        if target.key and (dup := await find_duplicate(session, source_key=target.key)):
            return JSONResponse(  # type: ignore[return-value]
                status_code=status.HTTP_409_CONFLICT,
                content={
                    "detail": f"Already in your library: {dup.artist} - {dup.title}",
                    "existing_track_id": str(dup.id),
                },
            )
        if active := await _active_job(session, IMPORT_URL, target.url):
            return JSONResponse(  # type: ignore[return-value]
                status_code=status.HTTP_200_OK, content=job_out(active).model_dump(mode="json")
            )
        job = Job(
            type=IMPORT_URL,
            payload={
                "input": body.url.strip(),
                "url": target.url,
                "source": target.source,
                "key": target.key,
                "needs_resolve": target.needs_resolve,
            },
        )
    session.add(job)
    await session.commit()
    await session.refresh(job, ["result_track"])
    return job_out(job)


@router.get("/jobs/{job_id}")
async def get_job(job_id: uuid.UUID, session: SessionDep, _: TokenDep) -> JobOut:
    job = await session.get(Job, job_id, options=[selectinload(Job.result_track)])
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    return job_out(job)


@router.get("/jobs")
async def list_jobs(
    session: SessionDep,
    _: TokenDep,
    status_: Annotated[list[JobStatus] | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 20,
    include_children: bool = False,
) -> list[JobOut]:
    """Recent jobs. The per-track jobs of a playlist import are hidden unless
    include_children=true (their parent reports the progress)."""
    stmt = select(Job).options(selectinload(Job.result_track))
    if not include_children:
        stmt = stmt.where(~Job.payload.has_key("parent_id"))
    if status_:
        stmt = stmt.where(Job.status.in_(status_))
    rows = await session.scalars(stmt.order_by(Job.created_at.desc()).limit(limit))
    return [job_out(j) for j in rows]


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: uuid.UUID, session: SessionDep, _: TokenDep) -> JobOut:
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    if job.status != "failed":
        raise HTTPException(status.HTTP_409_CONFLICT, "Only failed jobs can be retried")
    await session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(
            status="queued",
            progress=0,
            error=None,
            attempts=0,
            run_after=func.now(),
            started_at=None,
            finished_at=None,
        )
    )
    await session.commit()
    job = await session.get(
        Job, job_id, options=[selectinload(Job.result_track)], populate_existing=True
    )
    assert job is not None
    return job_out(job)


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: uuid.UUID, session: SessionDep, _: TokenDep) -> JobOut:
    """Cancel a queued import, or the remaining tracks of a playlist import (a download in
    progress finishes; the playlist is then filled with what was imported)."""
    job = await session.get(Job, job_id)
    if job is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Job not found")
    if job.status not in ("queued", "running"):
        raise HTTPException(status.HTTP_409_CONFLICT, "This import has already finished")
    try:
        await playlist_import.cancel(session, job)
    except playlist_import.PlaylistImportFailed as e:
        raise HTTPException(status.HTTP_409_CONFLICT, e.message) from None
    job = await session.get(
        Job, job_id, options=[selectinload(Job.result_track)], populate_existing=True
    )
    assert job is not None
    return job_out(job)
