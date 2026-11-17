from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from app.metadata import clean_artist, clean_title, guess_from_filename
from tests.audio_fixtures import make_audio, needs_ffmpeg

_freq = itertools.count(300, 7)  # distinct content per file -> distinct sha256


@pytest.fixture
def mp3(tmp_path: Path) -> Path:
    return make_audio(
        tmp_path / "Some Artist - Some Song (Official Video).mp3",
        freq=next(_freq),
        codec_args=("-c:a", "libmp3lame", "-b:a", "128k"),
    )


async def upload(client, auth, path: Path, **fields: str):  # type: ignore[no-untyped-def]
    with path.open("rb") as f:
        return await client.post(
            "/api/tracks/upload",
            headers=auth,
            files={"file": (path.name, f, "application/octet-stream")},
            data=fields,
        )


@needs_ffmpeg
async def test_upload_list_get_delete(client, auth, mp3: Path, music_dir: Path) -> None:
    r = await upload(client, auth, mp3)
    assert r.status_code == 201, r.text
    t = r.json()
    # metadata guessed from the file name, noise stripped
    assert t["artist"] == "Some Artist"
    assert t["title"] == "Some Song"
    assert t["mime_type"] == "audio/mpeg"
    assert t["source"] == "upload"
    assert 2.5 < t["duration_seconds"] < 3.5
    on_disk = music_dir / f"{t['id']}.mp3"
    assert on_disk.is_file()

    r = await client.get(f"/api/tracks/{t['id']}", headers=auth)
    assert r.status_code == 200

    r = await client.get("/api/tracks", params={"q": "some song"}, headers=auth)
    assert [x["id"] for x in r.json()["items"]] == [t["id"]]

    assert (await client.delete(f"/api/tracks/{t['id']}", headers=auth)).status_code == 204
    assert not on_disk.exists()
    assert (await client.get(f"/api/tracks/{t['id']}", headers=auth)).status_code == 404


@needs_ffmpeg
async def test_upload_uses_tags_and_form_fields(client, auth, tmp_path: Path) -> None:
    f = make_audio(
        tmp_path / "x.m4a",
        freq=next(_freq),
        codec_args=("-c:a", "aac", "-b:a", "96k"),
        tags={"title": "Tagged Title", "artist": "Tagged Artist", "album": "Tagged Album"},
    )
    r = await upload(client, auth, f, artist="Form Artist")
    assert r.status_code == 201, r.text
    t = r.json()
    assert (t["title"], t["artist"], t["album"]) == ("Tagged Title", "Form Artist", "Tagged Album")
    assert t["mime_type"] == "audio/mp4"
    assert t["codec"] == "aac"


@needs_ffmpeg
async def test_duplicate_upload_rejected(client, auth, mp3: Path) -> None:
    first = await upload(client, auth, mp3)
    assert first.status_code == 201
    r = await upload(client, auth, mp3)
    assert r.status_code == 409
    assert r.json()["existing_track_id"] == first.json()["id"]


@needs_ffmpeg
async def test_opus_is_converted_to_aac(client, auth, tmp_path: Path, music_dir: Path) -> None:
    f = make_audio(tmp_path / "a.opus", freq=next(_freq), codec_args=("-c:a", "libopus"))
    r = await upload(client, auth, f, title="Opus Song", artist="X")
    assert r.status_code == 201, r.text
    t = r.json()
    assert t["mime_type"] == "audio/mp4"
    assert t["codec"] == "aac"
    assert (music_dir / f"{t['id']}.m4a").is_file()
    # the same original file is still recognised as a duplicate after conversion
    assert (await upload(client, auth, f)).status_code == 409


async def test_upload_rejects_non_audio(client, auth, tmp_path: Path, music_dir: Path) -> None:
    fake = tmp_path / "evil.mp3"
    fake.write_bytes(b"<?php echo 'hi'; ?>" * 100)
    r = await upload(client, auth, fake)
    assert r.status_code == 415
    txt = tmp_path / "notes.txt"
    txt.write_text("hello")
    assert (await upload(client, auth, txt)).status_code == 415
    # nothing left behind in the temp dir
    assert list((music_dir / ".tmp").iterdir()) == []


async def test_upload_too_large(client, auth, tmp_path: Path, monkeypatch) -> None:
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "max_upload_mb", 1)
    big = tmp_path / "big.mp3"
    big.write_bytes(b"\0" * (2 * 1024 * 1024))
    assert (await upload(client, auth, big)).status_code == 413


async def test_upload_requires_auth(client, mp3_bytes=b"x") -> None:
    r = await client.post("/api/tracks/upload", files={"file": ("a.mp3", mp3_bytes)})
    assert r.status_code == 401


@needs_ffmpeg
async def test_edit_track(client, auth, mp3: Path) -> None:
    tid = (await upload(client, auth, mp3)).json()["id"]
    r = await client.patch(
        f"/api/tracks/{tid}", headers=auth, json={"title": "New", "album": "An Album"}
    )
    assert r.status_code == 200
    assert (r.json()["title"], r.json()["album"]) == ("New", "An Album")
    r = await client.patch(f"/api/tracks/{tid}", headers=auth, json={"album": ""})
    assert r.json()["album"] is None
    r = await client.patch(f"/api/tracks/{tid}", headers=auth, json={"artist": "   "})
    assert r.status_code == 422


@needs_ffmpeg
async def test_search_accent_insensitive(client, auth, tmp_path: Path) -> None:
    f = make_audio(tmp_path / "b.mp3", freq=next(_freq), codec_args=("-c:a", "libmp3lame"))
    tid = (await upload(client, auth, f, title="Déjà Vu", artist="Beyoncé")).json()["id"]
    for q in ("beyonce", "DEJA vu", "vu beyon"):
        r = await client.get("/api/tracks", params={"q": q}, headers=auth)
        assert tid in [x["id"] for x in r.json()["items"]], q
    r = await client.get("/api/tracks", params={"q": "100%"}, headers=auth)
    assert r.status_code == 200


@needs_ffmpeg
async def test_stream_range_and_signed_url(client, auth, mp3: Path) -> None:
    tid = (await upload(client, auth, mp3)).json()["id"]
    size = mp3.stat().st_size

    # bearer auth, full file
    r = await client.get(f"/api/tracks/{tid}/stream", headers=auth)
    assert r.status_code == 200
    assert r.headers["accept-ranges"] == "bytes"
    assert len(r.content) == size

    # Range request -> 206 Partial Content
    r = await client.get(f"/api/tracks/{tid}/stream", headers={**auth, "Range": "bytes=100-199"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 100-199/{size}"
    assert r.content == mp3.read_bytes()[100:200]

    # open-ended range
    r = await client.get(f"/api/tracks/{tid}/stream", headers={**auth, "Range": "bytes=1000-"})
    assert r.status_code == 206
    assert len(r.content) == size - 1000

    # no auth -> 401; signed URL works without a header; tampered signature -> 403
    assert (await client.get(f"/api/tracks/{tid}/stream")).status_code == 401
    url = (await client.get(f"/api/tracks/{tid}/stream-url", headers=auth)).json()["url"]
    r = await client.get(url, headers={"Range": "bytes=0-9"})
    assert r.status_code == 206
    assert (await client.get(url[:-1] + ("0" if url[-1] != "0" else "1"))).status_code == 403


def test_metadata_cleanup() -> None:
    assert clean_title("Song (Official Music Video)") == "Song"
    assert clean_title("Song [Lyrics] (HD)") == "Song"
    assert clean_title("Song (Remix)") == "Song (Remix)"
    assert clean_artist("Daft Punk - Topic") == "Daft Punk"
    assert clean_artist("DaftPunkVEVO") == "DaftPunk"
    assert guess_from_filename("01 - Artist - Title (Audio)") == ("Artist", "Title")
    assert guess_from_filename("just_a_title") == (None, "just a title")
