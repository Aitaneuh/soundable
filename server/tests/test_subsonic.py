from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import subprocess
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import pytest

from app.subsonic.mapping import album_id, artist_id, index_letter, parse_album_id
from tests.audio_fixtures import make_audio, needs_ffmpeg
from tests.conftest import SUBSONIC_PASSWORD, SUBSONIC_USER

NS = "{http://subsonic.org/restapi}"
_freq = itertools.count(3000, 17)
_run = itertools.count(1)


def token_auth(salt: str = "c19b2d") -> dict[str, str]:
    t = hashlib.md5((SUBSONIC_PASSWORD + salt).encode()).hexdigest()
    return {"u": SUBSONIC_USER, "t": t, "s": salt, "v": "1.13.0", "c": "pytest"}


def plain_auth() -> dict[str, str]:
    return {"u": SUBSONIC_USER, "p": SUBSONIC_PASSWORD, "v": "1.13.0", "c": "pytest"}


async def sjson(client, method: str, **params: Any) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    r = await client.get(f"/rest/{method}", params={**token_auth(), "f": "json", **params})
    assert r.status_code == 200, r.text
    return r.json()["subsonic-response"]


async def sxml(client, method: str, **params: Any) -> ET.Element:  # type: ignore[no-untyped-def]
    r = await client.get(f"/rest/{method}.view", params={**token_auth(), **params})
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/xml")
    return ET.fromstring(r.content)


@pytest.fixture
async def songs(client, auth, tmp_path: Path) -> list[dict[str, Any]]:  # type: ignore[no-untyped-def]
    """Three tracks by 'The Subsonic Band' (two on an album, one single) + one by 'Ébène'."""
    n = next(_run)  # unique artist names per test, the DB is shared
    band, other = f"The Subsonic Band {n}", f"Ébène {n}"
    specs = [
        ("Alpha", band, "First Album"),
        ("Beta", band, "First Album"),
        ("Gamma", band, ""),
        ("Delta", other, ""),
    ]
    out = []
    for title, artist, album in specs:
        f = make_audio(
            tmp_path / f"{title}.m4a",
            seconds=3,
            freq=next(_freq),
            codec_args=("-c:a", "aac", "-b:a", "192k"),
        )
        with f.open("rb") as fh:
            r = await client.post(
                "/api/tracks/upload",
                headers=auth,
                files={"file": (f.name, fh)},
                data={"title": title, "artist": artist, "album": album},
            )
        assert r.status_code == 201, r.text
        out.append(r.json())
    return out


# --- auth & envelope ----------------------------------------------------------------------


async def test_ping_token_auth_xml(client) -> None:
    root = await sxml(client, "ping")
    assert root.tag == f"{NS}subsonic-response"
    assert root.get("status") == "ok" and root.get("version") == "1.16.1"
    assert root.get("openSubsonic") == "true" and root.get("type") == "soundable"


async def test_ping_password_plain_and_hex(client) -> None:
    r = await client.get("/rest/ping", params={**plain_auth(), "f": "json"})
    assert r.json()["subsonic-response"]["status"] == "ok"
    hexpw = "enc:" + SUBSONIC_PASSWORD.encode().hex()
    r = await client.get("/rest/ping", params={**plain_auth(), "p": hexpw, "f": "json"})
    assert r.json()["subsonic-response"]["status"] == "ok"


async def test_ping_post_form(client) -> None:
    r = await client.post("/rest/ping.view", data={**token_auth(), "f": "json"})
    assert r.json()["subsonic-response"]["status"] == "ok"


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"u": SUBSONIC_USER, "p": "wrong"}, 40),
        ({"u": "nobody", "p": SUBSONIC_PASSWORD}, 40),
        ({"u": SUBSONIC_USER, "t": "0" * 32, "s": "abc"}, 40),
        ({"u": SUBSONIC_USER}, 10),
        ({}, 10),
    ],
)
async def test_auth_errors(client, params: dict[str, str], code: int) -> None:
    from app.security import login_limiter

    login_limiter().reset("subsonic:127.0.0.1")
    r = await client.get("/rest/ping", params={**params, "f": "json"})
    body = r.json()["subsonic-response"]
    assert body["status"] == "failed" and body["error"]["code"] == code
    root = ET.fromstring((await client.get("/rest/ping.view", params=params)).content)
    assert root.get("status") == "failed"
    assert root.find(f"{NS}error").get("code") == str(code)  # type: ignore[union-attr]
    login_limiter().reset("subsonic:127.0.0.1")


async def test_not_found_and_unknown(client) -> None:
    body = await sjson(client, "getSong", id="00000000-0000-0000-0000-000000000000")
    assert body["error"]["code"] == 70
    assert (await sjson(client, "getSong", id="garbage"))["error"]["code"] == 70
    assert (await sjson(client, "getSong"))["error"]["code"] == 10
    assert (await sjson(client, "getCoverArt", id="x"))["error"]["code"] == 70
    assert (await sjson(client, "noSuchMethod"))["status"] == "failed"


async def test_system_endpoints(client) -> None:
    assert (await sjson(client, "getLicense"))["license"]["valid"] is True
    ext = (await sjson(client, "getOpenSubsonicExtensions"))["openSubsonicExtensions"]
    assert {"name": "transcodeOffset", "versions": [1]} in ext
    assert (await sjson(client, "getMusicFolders"))["musicFolders"]["musicFolder"][0]["id"] == 1
    for m in (
        "getGenres",
        "getPodcasts",
        "getInternetRadioStations",
        "getSimilarSongs2",
        "scrobble",
    ):
        assert (await sjson(client, m, id="x"))["status"] == "ok", m
    # XML form of a list-with-scalars payload
    root = await sxml(client, "getOpenSubsonicExtensions")
    exts = root.findall(f"{NS}openSubsonicExtensions")
    assert exts and exts[0].find(f"{NS}versions").text == "1"  # type: ignore[union-attr]


# --- library model -------------------------------------------------------------------------


def test_ids_roundtrip() -> None:
    assert parse_album_id(album_id("A/B", None)) == ("A/B", None)
    assert parse_album_id(album_id("Ébène", "Disque 1")) == ("Ébène", "Disque 1")
    assert index_letter("The Beatles") == "B"
    assert index_letter("Ébène") == "E"
    assert index_letter("2Pac") == "#"


@needs_ffmpeg
async def test_browse_artists_albums_songs(client, songs) -> None:  # type: ignore[no-untyped-def]
    body = await sjson(client, "getArtists")
    idx = {i["name"]: i["artist"] for i in body["artists"]["index"]}
    band = next(a for a in idx["S"] if a["name"] == songs[0]["artist"])
    assert band["albumCount"] == 2  # "First Album" + synthetic "Singles"
    assert any(a["name"] == songs[3]["artist"] for a in idx["E"])

    art = (await sjson(client, "getArtist", id=band["id"]))["artist"]
    names = [a["name"] for a in art["album"]]
    assert names == ["First Album", "Singles"]
    first = art["album"][0]
    assert first["songCount"] == 2 and first["artistId"] == band["id"]
    assert "coverArt" not in first

    alb = (await sjson(client, "getAlbum", id=first["id"]))["album"]
    titles = [s["title"] for s in alb["song"]]
    assert titles == ["Alpha", "Beta"]
    s0 = alb["song"][0]
    assert s0["albumId"] == first["id"] and s0["artistId"] == band["id"]
    assert s0["suffix"] == "m4a" and s0["contentType"] == "audio/mp4" and s0["duration"] == 3
    assert "coverArt" not in s0

    # directory-style browsing
    d = (await sjson(client, "getMusicDirectory", id=band["id"]))["directory"]
    assert [c["title"] for c in d["child"]] == ["First Album", "Singles"]
    d = (await sjson(client, "getMusicDirectory", id=first["id"]))["directory"]
    assert [c["title"] for c in d["child"]] == ["Alpha", "Beta"]
    idx_body = await sjson(client, "getIndexes")
    assert "lastModified" in idx_body["indexes"]

    # XML shape for getAlbum
    root = await sxml(client, "getAlbum", id=first["id"])
    album_el = root.find(f"{NS}album")
    assert album_el is not None and album_el.get("name") == "First Album"
    assert [e.get("title") for e in album_el.findall(f"{NS}song")] == ["Alpha", "Beta"]

    assert (await sjson(client, "getArtist", id=artist_id("Nobody")))["error"]["code"] == 70


@needs_ffmpeg
async def test_album_lists_and_search(client, songs) -> None:  # type: ignore[no-untyped-def]
    lst = (await sjson(client, "getAlbumList2", type="alphabeticalByName", size=500))["albumList2"]
    names = [(a["name"], a["artist"]) for a in lst["album"]]
    assert ("First Album", songs[0]["artist"]) in names
    newest = (await sjson(client, "getAlbumList2", type="newest", size=1))["albumList2"]["album"]
    assert len(newest) == 1
    assert (await sjson(client, "getAlbumList2", type="bogus"))["status"] == "failed"

    # Amperfy-style full sync: query="" pages through every song with a stable order
    total = (await sjson(client, "getScanStatus"))["scanStatus"]["count"]
    seen: list[str] = []
    offset = 0
    while True:
        page = (
            await sjson(
                client,
                "search3",
                query="",
                artistCount=0,
                albumCount=0,
                songCount=2,
                songOffset=offset,
            )
        )["searchResult3"]
        got = [s["id"] for s in page.get("song", [])]
        if not got:
            break
        seen += got
        offset += len(got)
    assert len(seen) == len(set(seen)) == total

    other = songs[3]["artist"]  # "Ébène N": accent-insensitive match
    res = await sjson(client, "search3", query=other.replace("É", "e").replace("è", "e"))
    res = res["searchResult3"]
    assert [s["title"] for s in res["song"]] == ["Delta"]
    assert [a["name"] for a in res["artist"]] == [other]
    res = await sjson(client, "search3", query=f"first album {songs[0]['artist']}")
    assert [a["name"] for a in res["searchResult3"]["album"]] == ["First Album"]

    rnd = (await sjson(client, "getRandomSongs", size=2))["randomSongs"]["song"]
    assert len(rnd) == 2


@needs_ffmpeg
async def test_star_and_starred(client, songs) -> None:  # type: ignore[no-untyped-def]
    sid = songs[0]["id"]
    assert (await sjson(client, "star", id=sid))["status"] == "ok"
    starred = (await sjson(client, "getStarred2"))["starred2"]["song"]
    assert sid in [s["id"] for s in starred]
    assert (await sjson(client, "getSong", id=sid))["song"]["starred"].endswith("Z")
    await sjson(client, "unstar", id=sid)
    starred = (await sjson(client, "getStarred2"))["starred2"]["song"]
    assert sid not in [s["id"] for s in starred]


@needs_ffmpeg
async def test_playlists(client, songs) -> None:  # type: ignore[no-untyped-def]
    a, b, c, _ = (s["id"] for s in songs)
    r = await client.get(
        "/rest/createPlaylist",
        params=[
            *token_auth().items(),
            ("f", "json"),
            ("name", "Phone Mix"),
            ("songId", a),
            ("songId", b),
        ],
    )
    pl = r.json()["subsonic-response"]["playlist"]
    pid = pl["id"]
    assert pl["songCount"] == 2 and [e["title"] for e in pl["entry"]] == ["Alpha", "Beta"]

    # same name again -> suffixed instead of failing
    dup = (await sjson(client, "createPlaylist", name="Phone Mix"))["playlist"]
    assert dup["name"] == "Phone Mix (2)"

    r = await client.get(
        "/rest/updatePlaylist",
        params=[
            *token_auth().items(),
            ("f", "json"),
            ("playlistId", pid),
            ("name", "Phone Mix!"),
            ("songIndexToRemove", "0"),
            ("songIdToAdd", c),
        ],
    )
    assert r.json()["subsonic-response"]["status"] == "ok"
    pl = (await sjson(client, "getPlaylist", id=pid))["playlist"]
    assert pl["name"] == "Phone Mix!" and [e["title"] for e in pl["entry"]] == ["Beta", "Gamma"]

    lists = (await sjson(client, "getPlaylists"))["playlists"]["playlist"]
    mine = next(p for p in lists if p["id"] == pid)
    assert mine["songCount"] == 2 and mine["owner"] == SUBSONIC_USER

    root = await sxml(client, "getPlaylist", id=pid)
    assert len(root.find(f"{NS}playlist").findall(f"{NS}entry")) == 2  # type: ignore[union-attr]

    assert (await sjson(client, "deletePlaylist", id=pid))["status"] == "ok"
    assert (await sjson(client, "deletePlaylist", id=dup["id"]))["status"] == "ok"
    assert (await sjson(client, "getPlaylist", id=pid))["error"]["code"] == 70


# --- media -----------------------------------------------------------------------------------


@needs_ffmpeg
async def test_stream_raw_with_range(client, songs) -> None:  # type: ignore[no-untyped-def]
    sid = songs[0]["id"]
    r = await client.get("/rest/stream", params={**token_auth(), "id": sid})
    assert r.status_code == 200 and r.headers["content-type"] == "audio/mp4"
    size = len(r.content)
    r = await client.get(
        "/rest/stream.view",
        params={**token_auth(), "id": sid, "format": "raw"},
        headers={"Range": "bytes=10-19"},
    )
    assert r.status_code == 206 and r.headers["content-range"] == f"bytes 10-19/{size}"
    r = await client.get("/rest/download", params={**token_auth(), "id": sid})
    assert "attachment" in r.headers["content-disposition"]
    # 192 kbps source, limit 320 -> no transcoding
    r = await client.get("/rest/stream", params={**token_auth(), "id": sid, "maxBitRate": "320"})
    assert r.headers["content-type"] == "audio/mp4"


@needs_ffmpeg
async def test_stream_transcodes_to_mp3(client, songs, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    sid = songs[1]["id"]
    for params in (
        {"maxBitRate": "64"},
        {"format": "mp3"},
        {"maxBitRate": "96", "timeOffset": "1"},
    ):
        r = await client.get("/rest/stream", params={**token_auth(), "id": sid, **params})
        assert r.status_code == 200, params
        assert r.headers["content-type"] == "audio/mpeg"
        out = tmp_path / "t.mp3"
        out.write_bytes(r.content)
        probe = await asyncio.to_thread(
            subprocess.run,
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(out)],
            capture_output=True,
            check=True,
        )
        stream = json.loads(probe.stdout)["streams"][0]
        assert stream["codec_name"] == "mp3", params
        expected = 2.0 if "timeOffset" in params else 3.0
        assert abs(float(stream.get("duration", expected)) - expected) < 0.6, params


@pytest.mark.parametrize(
    "path",
    [
        "//rest/ping.view",
        "/rest/rest/ping.view",
        "/rest/ping.view/",
        "//rest//ping",
        "/music/rest/ping",
    ],
)
async def test_forgiving_paths(client, path: str) -> None:
    """Server URL typed with a trailing slash or with /rest included still works."""
    # full URL: httpx would read a bare "//rest/..." as a host name
    r = await client.get(f"http://test{path}", params={**token_auth(), "f": "json"})
    assert r.status_code == 200, path
    assert r.json()["subsonic-response"]["status"] == "ok"


def test_normalize_path_leaves_api_alone() -> None:
    from app.paths import normalize_path

    assert normalize_path("/api/tracks/x/stream") == "/api/tracks/x/stream"
    assert normalize_path("//api//health") == "/api/health"
    assert normalize_path("/rest/getSong.view") == "/rest/getSong.view"
