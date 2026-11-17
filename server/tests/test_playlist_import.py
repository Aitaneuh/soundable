from __future__ import annotations

import itertools
import uuid
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app import jobs
from app.db import get_sessionmaker
from app.importer import ImportFailed, playlist_entries
from app.models import Job
from app.worker import run_one
from tests.audio_fixtures import make_audio, needs_ffmpeg
from tests.test_imports import _drain_queue

_freq = itertools.count(5000, 19)
PL_URL = "https://www.youtube.com/playlist?list=PLtestplaylist0001"


def _vid(i: int, run: str) -> str:
    return f"{run}{i:02d}".ljust(11, "x")[:11]


class FakePlaylistDownloader:
    """Playlist of `n` YouTube videos; ids listed in `broken` fail permanently."""

    def __init__(self, tmp: Path, n: int, run: str, broken: set[str] | None = None) -> None:
        self.tmp, self.run, self.broken = tmp, run, broken or set()
        self.ids = [_vid(i, run) for i in range(n)]
        self.downloaded: list[str] = []

    def extract_playlist(self, url: str):  # type: ignore[no-untyped-def]
        entries = [
            {"id": v, "url": f"https://www.youtube.com/watch?v={v}", "title": f"Artist - Song {v}"}
            for v in self.ids
        ]
        entries.append(
            {
                "id": "zzzzzzzzzzz",
                "url": "https://www.youtube.com/watch?v=zzzzzzzzzzz",
                "title": "[Private video]",
            }
        )
        return playlist_entries({"_type": "playlist", "title": "Road Mix", "entries": entries})

    def extract(self, url: str) -> dict[str, Any]:
        vid = url.rsplit("=", 1)[-1]
        if vid in self.broken:
            raise ImportFailed("Video unavailable.", retryable=False)
        return {"id": vid, "title": f"Artist - Song {vid}", "uploader": "x"}

    def download(self, info: dict[str, Any], dest_dir: Path, progress) -> Path:  # type: ignore[no-untyped-def]
        self.downloaded.append(info["id"])
        progress(1.0)
        return make_audio(
            dest_dir / "audio.mp3", seconds=1, freq=next(_freq), codec_args=("-c:a", "libmp3lame")
        )


async def _claim() -> Job | None:
    async with get_sessionmaker()() as s:
        return await jobs.claim_next(s)


async def _drain(dl: FakePlaylistDownloader) -> list[Job]:
    ran = []
    while (job := await _claim()) is not None:
        ran.append(job)
        await run_one(job, dl)
    return ran


@needs_ffmpeg
async def test_playlist_import_end_to_end(client, auth, tmp_path: Path) -> None:
    await _drain_queue()
    run = uuid.uuid4().hex[:6]
    dl = FakePlaylistDownloader(tmp_path, n=4, run=run, broken={_vid(2, run)})

    # one of the playlist's videos is already in the library
    r = await client.post(
        "/api/imports", headers=auth, json={"url": f"https://youtu.be/{dl.ids[1]}"}
    )
    single = r.json()["id"]
    await run_one(await _claim(), dl)  # type: ignore[arg-type]
    existing_track = (await client.get(f"/api/jobs/{single}", headers=auth)).json()["track"]["id"]

    r = await client.post(
        "/api/imports", headers=auth, json={"url": PL_URL, "playlist_name": f"Road Mix {run}"}
    )
    assert r.status_code == 201, r.text
    parent = r.json()
    assert parent["kind"] == "playlist"
    # same playlist again while active -> same job
    r = await client.post("/api/imports", headers=auth, json={"url": PL_URL})
    assert r.json()["id"] == parent["id"]

    # parent runs first: spawns 3 children (index 1 already in library, private one skipped)
    first = await _claim()
    assert first is not None and str(first.id) == parent["id"]
    await run_one(first, dl)
    p = (await client.get(f"/api/jobs/{parent['id']}", headers=auth)).json()
    assert p["status"] == "running" and p["total"] == 4 and p["children"] == 3 and p["already"] == 1
    assert p["playlist_name"] == f"Road Mix {run}"

    # a single import pasted now jumps ahead of the playlist's tracks
    r = await client.post(
        "/api/imports", headers=auth, json={"url": f"https://youtu.be/{run}single"[:28]}
    )
    nxt = await _claim()
    assert nxt is not None and "parent_id" not in nxt.payload
    await run_one(nxt, dl)

    ran = await _drain(dl)
    assert [j.payload["index"] for j in ran] == [0, 2, 3]  # playlist order

    p = (await client.get(f"/api/jobs/{parent['id']}", headers=auth)).json()
    assert p["status"] == "done" and p["done"] == 2 and p["failed"] == 1
    assert "1 of 4" in p["error"]
    pl = (await client.get(f"/api/playlists/{p['playlist_id']}", headers=auth)).json()
    titles = [t["title"] for t in pl["tracks"]]
    assert titles == [f"Song {dl.ids[0]}", f"Song {dl.ids[1]}", f"Song {dl.ids[3]}"]
    assert pl["tracks"][1]["id"] == existing_track

    # children are hidden from the default job list
    listed = [j["id"] for j in (await client.get("/api/jobs?limit=50", headers=auth)).json()]
    assert parent["id"] in listed
    async with get_sessionmaker()() as s:
        kids = (
            await s.scalars(select(Job.id).where(Job.payload["parent_id"].astext == parent["id"]))
        ).all()
    assert kids and not set(map(str, kids)) & set(listed)


@needs_ffmpeg
async def test_playlist_import_cancel(client, auth, tmp_path: Path) -> None:
    await _drain_queue()
    run = uuid.uuid4().hex[:6]
    dl = FakePlaylistDownloader(tmp_path, n=3, run=run)
    url = "https://www.youtube.com/playlist?list=PLtestcancel00" + run
    parent = (await client.post("/api/imports", headers=auth, json={"url": url})).json()
    await run_one(await _claim(), dl)  # type: ignore[arg-type]  # spawn
    await run_one(await _claim(), dl)  # type: ignore[arg-type]  # first track
    r = await client.post(f"/api/jobs/{parent['id']}/cancel", headers=auth)
    assert r.status_code == 200
    p = r.json()
    assert p["status"] == "done" and p["done"] == 1 and p["failed"] == 2
    assert await _claim() is None
    assert (await client.post(f"/api/jobs/{parent['id']}/cancel", headers=auth)).status_code == 409


async def test_import_modes(client, auth) -> None:
    await _drain_queue()
    video_in_list = "https://www.youtube.com/watch?v=jNQXAC9Ixyz&list=PLmodes0000000001"
    r = await client.post(
        "/api/imports", headers=auth, json={"url": video_in_list, "mode": "playlist"}
    )
    assert r.status_code == 201 and r.json()["kind"] == "playlist"
    r = await client.post(
        "/api/imports", headers=auth, json={"url": video_in_list, "mode": "track"}
    )
    assert r.status_code == 201 and r.json()["kind"] == "track"
    r = await client.post("/api/imports", headers=auth, json={"url": PL_URL, "mode": "track"})
    assert r.status_code == 400
    r = await client.post(
        "/api/imports",
        headers=auth,
        json={"url": "https://youtu.be/jNQXAC9Iabc", "mode": "playlist"},
    )
    assert r.status_code == 400
    await _drain_queue()
