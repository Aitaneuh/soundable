"""Importing a whole YouTube playlist / SoundCloud set.

A parent job (type import_playlist) lists the entries (yt-dlp "flat" extraction, no
download), then spawns one ordinary import_url child job per track. Tracks already in the
library are recorded directly instead of spawning a child. When the last child finishes,
the parent is finalised: if a playlist name was given, that playlist is filled with the
resulting tracks in the original order (including ones that were already in the library).
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app import jobs
from app import playlists as pl_svc
from app.db import get_sessionmaker
from app.library import find_duplicate
from app.models import Job
from app.sources import ImportUrl

log = logging.getLogger(__name__)

MAX_PLAYLIST_TRACKS = 500


class PlaylistImportFailed(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


@dataclass
class PlaylistInfo:
    title: str
    entries: list[ImportUrl]
    skipped: int = 0  # private / deleted / unsupported entries


async def unique_playlist_name(session: AsyncSession, name: str) -> str:
    existing = {s.playlist.name.casefold() for s in await pl_svc.list_playlists(session)}
    candidate, n = pl_svc.clean_name(name), 2
    base = candidate
    while candidate.casefold() in existing:
        candidate, n = f"{base} ({n})", n + 1
    return candidate


def children_filter(parent_id: uuid.UUID) -> Any:
    return Job.payload["parent_id"].astext == str(parent_id)


async def spawn(job: Job, info: PlaylistInfo) -> int:
    """Create the playlist (if asked) and one child job per new track. Returns the number
    of children created."""
    sm = get_sessionmaker()
    payload = dict(job.payload)
    async with sm() as session:
        playlist_id = payload.get("playlist_id")
        if payload.get("playlist_name") and not playlist_id:
            name = await unique_playlist_name(session, payload["playlist_name"])
            playlist_id = str((await pl_svc.create_playlist(session, name)).id)
            payload["playlist_name"] = name
            # Remember it right away so a crash/retry doesn't create a second playlist.
            await jobs.merge_payload(session, job.id, playlist_id=playlist_id, playlist_name=name)

        existing: list[dict[str, Any]] = []
        children: list[Job] = []
        for index, target in enumerate(info.entries):
            dup = await find_duplicate(session, source_key=target.key) if target.key else None
            if dup is not None:
                existing.append({"index": index, "track_id": str(dup.id)})
                continue
            children.append(
                Job(
                    type=jobs.IMPORT_URL,
                    payload={
                        "input": target.url,
                        "url": target.url,
                        "source": target.source,
                        "key": target.key,
                        "needs_resolve": False,
                        "parent_id": str(job.id),
                        "index": index,
                    },
                )
            )
        session.add_all(children)
        parent = await session.get(Job, job.id, populate_existing=True)
        assert parent is not None
        parent.payload = {
            **parent.payload,
            "spawned": True,
            "title": info.title,
            "total": len(info.entries),
            "skipped": info.skipped,
            "children": len(children),
            "existing": existing,
            "done": 0,
            "failed": 0,
        }
        parent.progress = 1
        await session.commit()
    log.info(
        "playlist job %s: %d entries, %d new, %d already in library, %d skipped",
        job.id,
        len(info.entries),
        len(children),
        len(existing),
        info.skipped,
    )
    if not children:
        await on_child_finished(job.id)
    return len(children)


async def on_child_finished(parent_id: uuid.UUID) -> None:
    """Update the parent's counters; finalise it once every child is done or failed."""
    async with get_sessionmaker()() as session:
        parent = await session.scalar(select(Job).where(Job.id == parent_id).with_for_update())
        if parent is None or parent.status != "running":
            await session.rollback()
            return
        kids = list(await session.scalars(select(Job).where(children_filter(parent_id))))
        done = sum(1 for k in kids if k.status == "done")
        failed = sum(1 for k in kids if k.status == "failed")
        terminal = done + failed
        payload = {**parent.payload, "done": done, "failed": failed}
        if terminal < len(kids):
            parent.payload = payload
            parent.progress = max(1, min(99, int(100 * terminal / max(1, len(kids)))))
            await session.commit()
            return

        # Everything finished: assemble the playlist in the original order.
        by_index: dict[int, str] = {e["index"]: e["track_id"] for e in payload.get("existing", [])}
        already = len(by_index)
        lost = 0
        for k in kids:
            if k.result_track_id is not None:
                by_index[int(k.payload["index"])] = str(k.result_track_id)
                if k.status == "failed":  # failed as "already in your library"
                    already += 1
            elif k.status == "failed":
                lost += 1
        ordered = [uuid.UUID(by_index[i]) for i in sorted(by_index)]
        if pid := payload.get("playlist_id"):
            try:
                await pl_svc.set_tracks(session, uuid.UUID(pid), ordered)
            except pl_svc.PlaylistNotFound:
                log.info("playlist %s was deleted during the import", pid)
        imported = done
        payload.update(imported=imported, already=already, lost=lost)
        parent = await session.scalar(select(Job).where(Job.id == parent_id).with_for_update())
        assert parent is not None
        parent.payload = payload
        parent.status = "done"
        parent.progress = 100
        parent.finished_at = func.now()  # type: ignore[assignment]
        parent.error = (
            f"{lost} of {payload.get('total', len(kids))} track(s) couldn't be imported"
            if lost
            else None
        )
        await session.commit()
        log.info(
            "playlist job %s finished: %d new, %d already, %d failed",
            parent_id,
            imported,
            already,
            lost,
        )


async def finalize_orphans() -> None:
    """At worker start: parents whose children all finished while no worker was running."""
    async with get_sessionmaker()() as session:
        ids = list(
            await session.scalars(
                select(Job.id).where(
                    Job.type == jobs.IMPORT_PLAYLIST,
                    Job.status == "running",
                    Job.payload.has_key("spawned"),
                )
            )
        )
    for pid in ids:
        await on_child_finished(pid)


async def cancel(session: AsyncSession, job: Job) -> None:
    """Cancel queued work. A running download finishes; the parent then finalises."""
    if job.type == jobs.IMPORT_PLAYLIST:
        await session.execute(
            update(Job)
            .where(children_filter(job.id), Job.status == "queued")
            .values(status="failed", error="Cancelled", finished_at=func.now())
        )
        if not job.payload.get("spawned"):
            await session.execute(
                update(Job)
                .where(Job.id == job.id)
                .values(status="failed", error="Cancelled", finished_at=func.now())
            )
        await session.commit()
        if job.payload.get("spawned"):
            await asyncio.shield(on_child_finished(job.id))
        return
    if job.status != "queued":
        raise PlaylistImportFailed("Only queued imports can be cancelled", retryable=False)
    await session.execute(
        update(Job)
        .where(Job.id == job.id)
        .values(status="failed", error="Cancelled", finished_at=func.now())
    )
    await session.commit()
    if parent := job.payload.get("parent_id"):
        await on_child_finished(uuid.UUID(parent))
