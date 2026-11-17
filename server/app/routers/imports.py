from __future__ import annotations

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import selectinload

from app.deps import SessionDep, TokenDep
from app.jobs import IMPORT_URL, job_out
from app.library import find_duplicate
from app.models import Job
from app.schemas import ErrorOut, JobOut
from app.sources import UnsupportedUrl, parse_import_url

router = APIRouter(prefix="/api", tags=["imports"])

JobStatus = Literal["queued", "running", "done", "failed"]


class ImportIn(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


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
    """
    try:
        target = parse_import_url(body.url)
    except UnsupportedUrl as e:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(e)) from None

    if target.key and (dup := await find_duplicate(session, source_key=target.key)):
        return JSONResponse(  # type: ignore[return-value]
            status_code=status.HTTP_409_CONFLICT,
            content={
                "detail": f"Already in your library: {dup.artist} - {dup.title}",
                "existing_track_id": str(dup.id),
            },
        )

    active = await session.scalar(
        select(Job)
        .options(selectinload(Job.result_track))
        .where(
            Job.type == IMPORT_URL,
            Job.status.in_(("queued", "running")),
            Job.payload["url"].astext == target.url,
        )
        .limit(1)
    )
    if active:
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
) -> list[JobOut]:
    stmt = select(Job).options(selectinload(Job.result_track))
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
