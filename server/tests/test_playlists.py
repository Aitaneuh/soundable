from __future__ import annotations

import itertools
from pathlib import Path

import pytest

from tests.audio_fixtures import make_audio, needs_ffmpeg

_freq = itertools.count(2000, 11)
pytestmark = needs_ffmpeg


@pytest.fixture
async def three_tracks(client, auth, tmp_path: Path) -> list[str]:  # type: ignore[no-untyped-def]
    ids = []
    for i in range(3):
        f = make_audio(
            tmp_path / f"t{i}.mp3", seconds=2, freq=next(_freq), codec_args=("-c:a", "libmp3lame")
        )
        with f.open("rb") as fh:
            r = await client.post(
                "/api/tracks/upload",
                headers=auth,
                files={"file": (f.name, fh)},
                data={"title": f"PL Song {i}", "artist": "PL Artist"},
            )
        assert r.status_code == 201, r.text
        ids.append(r.json()["id"])
    return ids


def titles(p: dict) -> list[str]:  # type: ignore[type-arg]
    return [t["title"][-1] for t in p["tracks"]]


async def test_playlist_lifecycle(client, auth, three_tracks: list[str]) -> None:
    a, b, c = three_tracks
    r = await client.post(
        "/api/playlists", headers=auth, json={"name": "  Road   Trip ", "track_ids": [a, b]}
    )
    assert r.status_code == 201, r.text
    p = r.json()
    pid = p["id"]
    assert p["name"] == "Road Trip" and titles(p) == ["0", "1"] and p["track_count"] == 2

    # case-insensitive unique names
    r = await client.post("/api/playlists", headers=auth, json={"name": "road trip"})
    assert r.status_code == 409

    # append, insert at position, duplicates allowed
    p = (
        await client.post(f"/api/playlists/{pid}/tracks", headers=auth, json={"track_ids": [c, a]})
    ).json()
    assert titles(p) == ["0", "1", "2", "0"]
    p = (
        await client.post(
            f"/api/playlists/{pid}/tracks", headers=auth, json={"track_ids": [c], "position": 0}
        )
    ).json()
    assert titles(p) == ["2", "0", "1", "2", "0"]

    # remove by positions, move, replace order
    r = await client.delete(
        f"/api/playlists/{pid}/tracks", headers=auth, params={"positions": [0, 4]}
    )
    assert titles(r.json()) == ["0", "1", "2"]
    r = await client.post(
        f"/api/playlists/{pid}/move", headers=auth, json={"from_position": 2, "to_position": 0}
    )
    assert titles(r.json()) == ["2", "0", "1"]
    r = await client.put(f"/api/playlists/{pid}/tracks", headers=auth, json={"track_ids": [b, b]})
    assert titles(r.json()) == ["1", "1"]

    # errors
    r = await client.delete(f"/api/playlists/{pid}/tracks", headers=auth, params={"positions": [9]})
    assert r.status_code == 400
    bogus = "00000000-0000-0000-0000-000000000000"
    r = await client.post(f"/api/playlists/{pid}/tracks", headers=auth, json={"track_ids": [bogus]})
    assert r.status_code == 400
    assert (await client.get(f"/api/playlists/{bogus}", headers=auth)).status_code == 404

    # rename + list
    r = await client.patch(
        f"/api/playlists/{pid}", headers=auth, json={"name": "Chill", "comment": "evening"}
    )
    assert (r.json()["name"], r.json()["comment"]) == ("Chill", "evening")
    lst = (await client.get("/api/playlists", headers=auth)).json()
    mine = next(x for x in lst if x["id"] == pid)
    assert mine["track_count"] == 2 and mine["duration_seconds"] > 3

    # deleting a track removes its entries; deleting the playlist works
    assert (await client.delete(f"/api/tracks/{b}", headers=auth)).status_code == 204
    assert (await client.get(f"/api/playlists/{pid}", headers=auth)).json()["tracks"] == []
    assert (await client.delete(f"/api/playlists/{pid}", headers=auth)).status_code == 204
    assert (await client.get(f"/api/playlists/{pid}", headers=auth)).status_code == 404


async def test_playlists_require_auth(client) -> None:
    assert (await client.get("/api/playlists")).status_code == 401
