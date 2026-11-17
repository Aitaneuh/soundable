"""Background worker: `python -m app.worker`.

Polls the jobs table, claims one job at a time with SKIP LOCKED and runs it. Imports are
network-bound and mostly sequential by nature (one YouTube download at a time is also
less likely to trigger rate limits), so one job at a time is deliberate.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal

from app import jobs
from app.config import get_settings
from app.db import get_engine, get_sessionmaker
from app.importer import Downloader, ImportFailed, YtDlpDownloader, process_import
from app.library import DuplicateTrackError, cleanup_stale_tmp
from app.models import Job

log = logging.getLogger("app.worker")

POLL_SECONDS = 2.0


async def run_one(job: Job, downloader: Downloader) -> None:
    sm = get_sessionmaker()
    log.info("job %s attempt %d: %s", job.id, job.attempts, job.payload.get("url"))
    try:
        if job.type != jobs.IMPORT_URL:
            raise ImportFailed(f"unknown job type {job.type!r}", retryable=False)
        track_id = await process_import(job.id, dict(job.payload), downloader)
    except DuplicateTrackError as e:
        async with sm() as session:
            await jobs.fail(
                session,
                job.id,
                f"Already in your library: {e.existing.artist} - {e.existing.title}",
                retryable=False,
                attempts=job.attempts,
                track_id=e.existing.id,
            )
        log.info("job %s: duplicate of %s", job.id, e.existing.id)
        return
    except ImportFailed as e:
        async with sm() as session:
            requeued = await jobs.fail(
                session, job.id, e.message, retryable=e.retryable, attempts=job.attempts
            )
        log.warning("job %s failed (%s): %s", job.id, "retry" if requeued else "final", e.message)
        return
    except Exception as e:
        log.exception("job %s crashed", job.id)
        async with sm() as session:
            await jobs.fail(
                session,
                job.id,
                f"Internal error: {e.__class__.__name__}: {e}"[:500],
                retryable=True,
                attempts=job.attempts,
            )
        return
    async with sm() as session:
        await jobs.finish(session, job.id, track_id)
    log.info("job %s done -> track %s", job.id, track_id)


async def run(stop: asyncio.Event, downloader: Downloader | None = None) -> None:
    downloader = downloader or YtDlpDownloader()
    sm = get_sessionmaker()
    get_settings().music_dir.mkdir(parents=True, exist_ok=True)
    cleanup_stale_tmp()
    async with sm() as session:
        if n := await jobs.requeue_stale(session):
            log.warning("re-queued %d job(s) left running by a previous worker", n)
    log.info("worker started")
    while not stop.is_set():
        try:
            async with sm() as session:
                job = await jobs.claim_next(session)
        except Exception:
            log.exception("could not poll the jobs table; retrying")
            job = None
        if job is None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), POLL_SECONDS)
            continue
        await run_one(job, downloader)
    log.info("worker stopped")


async def main() -> None:
    logging.basicConfig(
        level=get_settings().log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # Windows dev runs
            loop.add_signal_handler(sig, stop.set)
    try:
        await run(stop)
    finally:
        await get_engine().dispose()


if __name__ == "__main__":
    asyncio.run(main())
