from __future__ import annotations

import asyncio
import itertools
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from app import jobs
from app.db import get_sessionmaker
from app.importer import ImportFailed, classify_error, track_meta
from app.models import Job
from app.sources import UnsupportedUrl, parse_import_url
from app.worker import run_one
from tests.audio_fixtures import make_audio, needs_ffmpeg

# --- URL parsing -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "url", "key"),
    [
        (
            "https://www.youtube.com/watch?v=jNQXAC9IVRw",
            "https://www.youtube.com/watch?v=jNQXAC9IVRw",
            "youtube:jNQXAC9IVRw",
        ),
        (
            "youtu.be/jNQXAC9IVRw?t=10",
            "https://www.youtube.com/watch?v=jNQXAC9IVRw",
            "youtube:jNQXAC9IVRw",
        ),
        (
            "https://music.youtube.com/watch?v=jNQXAC9IVRw&si=abc",
            "https://www.youtube.com/watch?v=jNQXAC9IVRw",
            "youtube:jNQXAC9IVRw",
        ),
        (
            "https://m.youtube.com/shorts/jNQXAC9IVRw",
            "https://www.youtube.com/watch?v=jNQXAC9IVRw",
            "youtube:jNQXAC9IVRw",
        ),
        # a video inside a playlist: just the video
        (
            "https://www.youtube.com/watch?v=jNQXAC9IVRw&list=PL123&index=2",
            "https://www.youtube.com/watch?v=jNQXAC9IVRw",
            "youtube:jNQXAC9IVRw",
        ),
        (
            "https://soundcloud.com/forss/flickermood?utm_source=x",
            "https://soundcloud.com/forss/flickermood",
            None,
        ),
        (
            "https://m.soundcloud.com/forss/flickermood",
            "https://soundcloud.com/forss/flickermood",
            None,
        ),
        ("https://soundcloud.com/a/b/s-SeCrEt", "https://soundcloud.com/a/b/s-SeCrEt", None),
    ],
)
def test_parse_ok(raw: str, url: str, key: str | None) -> None:
    target = parse_import_url(raw)
    assert (target.url, target.key) == (url, key)


def test_parse_short_soundcloud() -> None:
    t = parse_import_url("https://on.soundcloud.com/AbC123")
    assert t.needs_resolve and t.url == "https://on.soundcloud.com/AbC123"


@pytest.mark.parametrize(
    "raw",
    [
        "https://www.youtube.com/playlist?list=PL123",
        "https://www.youtube.com/watch?list=PL123",
        "https://soundcloud.com/forss/sets/soundtracks",
        "https://soundcloud.com/forss",
        "https://soundcloud.com/forss/likes",
        "https://evil.com/watch?v=jNQXAC9IVRw",
        "https://youtube.com.evil.com/watch?v=jNQXAC9IVRw",
        "https://user:pw@www.youtube.com/watch?v=jNQXAC9IVRw",
        "https://www.youtube.com:8443/watch?v=jNQXAC9IVRw",
        "file:///etc/passwd",
        "http://169.254.169.254/latest/meta-data",
        "https://www.youtube.com/watch?v=short",
        "two links https://youtu.be/jNQXAC9IVRw",
    ],
)
def test_parse_rejected(raw: str) -> None:
    with pytest.raises(UnsupportedUrl):
        parse_import_url(raw)


# --- metadata & errors ---------------------------------------------------------------------


def test_track_meta() -> None:
    assert track_meta({"track": "Song", "artist": "Band", "title": "x"}).title == "Song"
    m = track_meta({"title": "Band - Song (Official Video)", "uploader": "BandVEVO"})
    assert (m.artist, m.title) == ("Band", "Song")
    m = track_meta({"title": "Just A Song [HD]", "uploader": "Some Channel - Topic"})
    assert (m.artist, m.title) == ("Some Channel", "Just A Song")


@pytest.mark.parametrize(
    ("raw", "fragment", "retryable"),
    [
        ("ERROR: [youtube] abc: Sign in to confirm you're not a bot.", "bot check", False),
        (
            "ERROR: [youtube] abc: Video unavailable. This video has been removed",
            "unavailable",
            False,
        ),
        (
            "ERROR: [youtube] abc: Private video. Sign in if you've been granted access",
            "private",
            False,
        ),
        (
            "ERROR: [soundcloud] 123: Unable to download JSON metadata: HTTP Error 404: Not Found",
            "not found",
            False,
        ),
        (
            "ERROR: unable to download video data: HTTP Error 503: Service Unavailable",
            "Network",
            True,
        ),
        ("ERROR: [youtube] abc: Read timed out.", "Network", True),
        ("ERROR: something new and weird", "something new and weird", True),
    ],
)
def test_classify_error(raw: str, fragment: str, retryable: bool) -> None:
    e = classify_error(raw)
    assert fragment.lower() in e.message.lower()
    assert e.retryable is retryable


# --- API ---------------------------------------------------------------------------------


async def test_import_api_flow(client, auth) -> None:
    r = await client.post(
        "/api/imports", headers=auth, json={"url": "https://youtu.be/aaaaaaaaaaa"}
    )
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["status"] == "queued" and job["url"].endswith("v=aaaaaaaaaaa")

    # same link while queued -> same job
    r = await client.post(
        "/api/imports", headers=auth, json={"url": "https://www.youtube.com/watch?v=aaaaaaaaaaa"}
    )
    assert r.status_code == 200 and r.json()["id"] == job["id"]

    r = await client.get(f"/api/jobs/{job['id']}", headers=auth)
    assert r.status_code == 200
    r = await client.get("/api/jobs", headers=auth, params={"status": "queued"})
    assert job["id"] in [j["id"] for j in r.json()]

    r = await client.post("/api/imports", headers=auth, json={"url": "https://evil.com/x"})
    assert r.status_code == 400
    r = await client.post(
        "/api/imports", headers=auth, json={"url": "https://www.youtube.com/playlist?list=PL1"}
    )
    assert r.status_code == 400 and "playlist" in r.json()["detail"].lower()
    assert (
        await client.post("/api/imports", json={"url": "https://youtu.be/x"})
    ).status_code == 401
    await _drain_queue()


async def _drain_queue() -> None:
    """Mark leftover queued jobs failed so tests don't pick each other's jobs."""
    async with get_sessionmaker()() as s:
        for j in (await s.scalars(select(Job).where(Job.status.in_(("queued", "running"))))).all():
            j.status = "failed"
        await s.commit()


# --- worker with a fake downloader ----------------------------------------------------------

_freq = itertools.count(900, 13)


class FakeDownloader:
    def __init__(
        self,
        tmp: Path,
        *,
        codec: str = "libopus",
        ext: str = "webm",
        fail: ImportFailed | None = None,
        info: dict[str, Any] | None = None,
    ) -> None:
        self.tmp, self.codec, self.ext, self.fail = tmp, codec, ext, fail
        self.info = info or {
            "id": uuid.uuid4().hex[:11],
            "title": "Fake Artist - Fake Song (Official Audio)",
            "uploader": "x",
        }
        self.progress: list[float] = []

    def extract(self, url: str) -> dict[str, Any]:
        if self.fail:
            raise self.fail
        return self.info

    def download(self, info: dict[str, Any], dest_dir: Path, progress) -> Path:  # type: ignore[no-untyped-def]
        for f in (0.25, 0.5, 1.0):
            progress(f)
            self.progress.append(f)
        return make_audio(
            dest_dir / f"audio.{self.ext}", freq=next(_freq), codec_args=("-c:a", self.codec)
        )


async def _enqueue(client, auth, url: str) -> str:  # type: ignore[no-untyped-def]
    r = await client.post("/api/imports", headers=auth, json={"url": url})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def _claim() -> Job:
    async with get_sessionmaker()() as s:
        job = await jobs.claim_next(s)
    assert job is not None
    return job


@needs_ffmpeg
async def test_worker_imports_and_converts(client, auth, tmp_path: Path, music_dir: Path) -> None:
    await _drain_queue()
    vid = "bbbbbbbbbbb"
    jid = await _enqueue(client, auth, f"https://youtu.be/{vid}")
    dl = FakeDownloader(
        tmp_path,
        info={"id": vid, "title": "Fake Artist - Fake Song (Official Audio)", "uploader": "x"},
    )
    await run_one(await _claim(), dl)
    await asyncio.sleep(0.2)

    job = (await client.get(f"/api/jobs/{jid}", headers=auth)).json()
    assert job["status"] == "done", job
    assert job["progress"] == 100
    assert (job["title"], job["artist"]) == ("Fake Song", "Fake Artist")
    t = job["track"]
    assert t["source"] == "youtube" and t["mime_type"] == "audio/mp4" and t["codec"] == "aac"
    assert (music_dir / f"{t['id']}.m4a").is_file()
    assert not list((music_dir / ".tmp").glob("job-*"))  # work dir cleaned up

    # importing the same video again is refused up front
    r = await client.post(
        "/api/imports", headers=auth, json={"url": f"https://www.youtube.com/watch?v={vid}"}
    )
    assert r.status_code == 409 and r.json()["existing_track_id"] == t["id"]


@needs_ffmpeg
async def test_worker_soundcloud_duplicate_detected_after_extract(
    client, auth, tmp_path: Path
) -> None:
    await _drain_queue()
    info = {"id": "424242", "title": "SC Song", "uploader": "SC Artist"}
    jid = await _enqueue(client, auth, "https://soundcloud.com/sc-artist/sc-song")
    await run_one(
        await _claim(), FakeDownloader(tmp_path, codec="libmp3lame", ext="mp3", info=info)
    )
    first = (await client.get(f"/api/jobs/{jid}", headers=auth)).json()
    assert first["status"] == "done" and first["track"]["mime_type"] == "audio/mpeg"

    # same track via another URL form (the key is the SoundCloud track id)
    jid2 = await _enqueue(client, auth, "https://m.soundcloud.com/sc-artist/sc-song-other-slug")
    await run_one(await _claim(), FakeDownloader(tmp_path, info=info))
    second = (await client.get(f"/api/jobs/{jid2}", headers=auth)).json()
    assert second["status"] == "failed"
    assert "already in your library" in second["error"].lower()
    assert second["track"]["id"] == first["track"]["id"]


async def test_worker_permanent_and_retryable_failures(client, auth, tmp_path: Path) -> None:
    await _drain_queue()
    jid = await _enqueue(client, auth, "https://youtu.be/ccccccccccc")
    await run_one(
        await _claim(),
        FakeDownloader(tmp_path, fail=ImportFailed("Video unavailable.", retryable=False)),
    )
    job = (await client.get(f"/api/jobs/{jid}", headers=auth)).json()
    assert (job["status"], job["error"]) == ("failed", "Video unavailable.")

    jid = await _enqueue(client, auth, "https://youtu.be/ddddddddddd")
    await run_one(
        await _claim(),
        FakeDownloader(tmp_path, fail=ImportFailed("Network problem.", retryable=True)),
    )
    job = (await client.get(f"/api/jobs/{jid}", headers=auth)).json()
    assert job["status"] == "queued" and "retrying" in job["error"]
    # backoff: not claimable right now
    async with get_sessionmaker()() as s:
        assert await jobs.claim_next(s) is None

    # manual retry of a failed job
    async with get_sessionmaker()() as s:
        await jobs.fail(s, uuid.UUID(jid), "boom", retryable=False, attempts=3)
    r = await client.post(f"/api/jobs/{jid}/retry", headers=auth)
    assert r.status_code == 200 and r.json()["status"] == "queued"
    await _drain_queue()


async def test_skip_locked_claims_distinct_jobs(client, auth) -> None:
    await _drain_queue()
    ids = {await _enqueue(client, auth, f"https://youtu.be/{c * 11}") for c in "efg"}
    sm = get_sessionmaker()

    async def claim() -> Job | None:
        async with sm() as s:
            return await jobs.claim_next(s)

    claimed = await asyncio.gather(*(claim() for _ in range(5)))
    got = [str(j.id) for j in claimed if j is not None]
    assert sorted(got) == sorted(ids)  # each job claimed exactly once
    await _drain_queue()
