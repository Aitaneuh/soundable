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
import uuid

from app import jobs, playlist_import
from app.config import get_settings
from app.db import get_engine, get_sessionmaker
from app.importer import Downloader, ImportFailed, YtDlpDownloader, process_import
from app.library import DuplicateTrackError, cleanup_stale_tmp
from app.models import Job

log = logging.getLogger("app.worker")

POLL_SECONDS = 2.0


async def run_one(job: Job, downloader: Downloader) -> None:
    log.info("job %s (%s) attempt %d: %s", job.id, job.type, job.attempts, job.payload.get("url"))
    if job.type == jobs.IMPORT_PLAYLIST:
        await _run_playlist(job, downloader)
    elif job.type == jobs.IMPORT_URL:
        await _run_track(job, downloader)
    else:
        async with get_sessionmaker()() as session:
            await jobs.fail(
                session,
                job.id,
                f"unknown job type {job.type!r}",
                retryable=False,
                attempts=job.attempts,
            )


async def _run_playlist(job: Job, downloader: Downloader) -> None:
    sm = get_sessionmaker()
    try:
        info = await asyncio.to_thread(downloader.extract_playlist, job.payload["url"])
        if not info.entries:
            raise ImportFailed(
                "The playlist is empty, or none of its tracks are available.", retryable=False
            )
        await playlist_import.spawn(job, info)
    except ImportFailed as e:
        async with sm() as session:
            await jobs.fail(
                session, job.id, e.message, retryable=e.retryable, attempts=job.attempts
            )
        log.warning("playlist job %s failed: %s", job.id, e.message)
    except Exception as e:
        log.exception("playlist job %s crashed", job.id)
        async with sm() as session:
            await jobs.fail(
                session,
                job.id,
                f"Internal error: {e.__class__.__name__}: {e}"[:500],
                retryable=True,
                attempts=job.attempts,
            )


async def _run_track(job: Job, downloader: Downloader) -> None:
    sm = get_sessionmaker()
    terminal = True
    try:
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
    except ImportFailed as e:
        async with sm() as session:
            requeued = await jobs.fail(
                session, job.id, e.message, retryable=e.retryable, attempts=job.attempts
            )
        terminal = not requeued
        log.warning("job %s failed (%s): %s", job.id, "retry" if requeued else "final", e.message)
    except Exception as e:
        log.exception("job %s crashed", job.id)
        async with sm() as session:
            requeued = await jobs.fail(
                session,
                job.id,
                f"Internal error: {e.__class__.__name__}: {e}"[:500],
                retryable=True,
                attempts=job.attempts,
            )
        terminal = not requeued
    else:
        async with sm() as session:
            await jobs.finish(session, job.id, track_id)
        log.info("job %s done -> track %s", job.id, track_id)
    if terminal and (parent := job.payload.get("parent_id")):
        await playlist_import.on_child_finished(uuid.UUID(parent))


async def run(stop: asyncio.Event, downloader: Downloader | None = None) -> None:
    downloader = downloader or YtDlpDownloader()
    sm = get_sessionmaker()
    get_settings().music_dir.mkdir(parents=True, exist_ok=True)
    cleanup_stale_tmp()
    async with sm() as session:
        if n := await jobs.requeue_stale(session):
            log.warning("re-queued %d job(s) left running by a previous worker", n)
    await playlist_import.finalize_orphans()
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
