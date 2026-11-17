"""Backup + restore round trip. Needs pg_dump/psql on PATH (present in the Docker image
and on CI runners); skipped otherwise."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from app import backup
from app.config import get_settings

pytestmark = pytest.mark.skipif(
    not (shutil.which("pg_dump") and shutil.which("psql")), reason="PostgreSQL client tools missing"
)


async def test_backup_and_restore_roundtrip(client, auth, tmp_path: Path, music_dir: Path) -> None:
    s = get_settings().model_copy(update={"backup_dir": tmp_path / "backups", "backup_keep": 2})
    (music_dir / "orphan-test.mp3").write_bytes(b"x" * 10)

    r = await client.post("/api/playlists", headers=auth, json={"name": "Backup Test PL"})
    assert r.status_code == 201
    pid = r.json()["id"]

    status = await asyncio.to_thread(backup.run_backup, s)
    assert status["ok"], status
    assert (s.backup_dir / "music" / "orphan-test.mp3").is_file()
    for _ in range(2):
        await asyncio.to_thread(backup.run_backup, s)
    assert len(backup.list_dumps(s)) == 2  # pruned to BACKUP_KEEP

    # damage: delete the playlist and a music file, then restore
    assert (await client.delete(f"/api/playlists/{pid}", headers=auth)).status_code == 204
    (music_dir / "orphan-test.mp3").unlink()
    await asyncio.to_thread(backup.restore, "latest", force=True, s=s)
    assert (await client.get(f"/api/playlists/{pid}", headers=auth)).json()[
        "name"
    ] == "Backup Test PL"
    assert (music_dir / "orphan-test.mp3").is_file()
    await client.delete(f"/api/playlists/{pid}", headers=auth)
