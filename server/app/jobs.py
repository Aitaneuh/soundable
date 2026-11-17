"""Postgres-backed job queue primitives (claimed with SELECT ... FOR UPDATE SKIP LOCKED)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Job
from app.schemas import JobOut, TrackOut

IMPORT_URL = "import_url"
IMPORT_PLAYLIST = "import_playlist"
MAX_ATTEMPTS = 3
BACKOFF = (timedelta(seconds=30), timedelta(minutes=3))  # before attempt 2 and 3
# A running job whose row hasn't been touched for this long belongs to a dead worker.
STALE_AFTER = timedelta(minutes=10)


def job_out(job: Job) -> JobOut:
    p = job.payload or {}
    return JobOut(
        id=job.id,
        type=job.type,
        kind="playlist" if job.type == IMPORT_PLAYLIST else "track",
        parent_id=p.get("parent_id"),
        total=p.get("total"),
        children=p.get("children"),
        done=p.get("done"),
        failed=p.get("failed"),
        already=p.get("already", len(p.get("existing", [])) if "existing" in p else None),
        playlist_id=p.get("playlist_id"),
        playlist_name=p.get("playlist_name"),
        status=job.status,  # type: ignore[arg-type]
        progress=job.progress,
        url=p.get("url"),
        source=p.get("source"),
        title=p.get("title"),
        artist=p.get("artist"),
        error=job.error,
        attempts=job.attempts,
        track=TrackOut.model_validate(job.result_track) if job.result_track else None,
        created_at=job.created_at,
        started_at=job.started_at,
        finished_at=job.finished_at,
    )


async def claim_next(session: AsyncSession) -> Job | None:
    """Atomically take the oldest runnable job. Safe with several workers."""
    job = await session.scalar(
        select(Job)
        .where(Job.status == "queued", Job.run_after <= func.now())
        # Single imports go before the tracks of a big playlist; playlist tracks in order.
        .order_by(
            Job.payload.has_key("parent_id"),
            Job.run_after,
            Job.created_at,
            Job.payload["index"].as_integer(),
        )
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    if job is None:
        await session.rollback()
        return None
    job.status = "running"
    job.attempts += 1
    job.progress = 0
    job.started_at = datetime.now(UTC)
    job.error = None
    await session.commit()
    return job


async def requeue_stale(session: AsyncSession) -> int:
    """Jobs left 'running' by a worker that died (crash, container kill) go back to the queue."""
    res = await session.execute(
        update(Job)
        .where(
            Job.status == "running",
            Job.updated_at < func.now() - STALE_AFTER,
            # a spawned playlist parent stays "running" until its children finish
            or_(Job.type != IMPORT_PLAYLIST, ~Job.payload.has_key("spawned")),
        )
        .values(status="queued", run_after=func.now())
    )
    await session.commit()
    return res.rowcount or 0  # type: ignore[attr-defined]


async def set_progress(session: AsyncSession, job_id: uuid.UUID, progress: int) -> None:
    await session.execute(
        update(Job).where(Job.id == job_id).values(progress=max(0, min(100, progress)))
    )
    await session.commit()


async def merge_payload(session: AsyncSession, job_id: uuid.UUID, **values: Any) -> None:
    job = await session.get(Job, job_id, populate_existing=True)
    if job is not None:
        job.payload = {**job.payload, **values}
        await session.commit()


async def finish(session: AsyncSession, job_id: uuid.UUID, track_id: uuid.UUID) -> None:
    await session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(
            status="done",
            progress=100,
            error=None,
            result_track_id=track_id,
            finished_at=func.now(),
        )
    )
    await session.commit()


async def fail(
    session: AsyncSession,
    job_id: uuid.UUID,
    message: str,
    *,
    retryable: bool,
    attempts: int,
    track_id: uuid.UUID | None = None,
) -> bool:
    """Record a failure. Returns True if the job was re-queued for another attempt."""
    if retryable and attempts < MAX_ATTEMPTS:
        delay = BACKOFF[min(attempts - 1, len(BACKOFF) - 1)]
        await session.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(
                status="queued",
                progress=0,
                error=f"{message} (attempt {attempts}/{MAX_ATTEMPTS}, retrying)",
                run_after=func.now() + delay,
            )
        )
        await session.commit()
        return True
    await session.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(
            status="failed",
            error=message,
            result_track_id=track_id,
            finished_at=func.now(),
        )
    )
    await session.commit()
    return False
