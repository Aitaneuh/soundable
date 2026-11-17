"""Backups: `python -m app.backup <command>`.

  loop                 run forever: a backup every BACKUP_INTERVAL_HOURS (the `backup` service)
  run                  one backup now
  list                 list database dumps
  restore [NAME|latest] [--force]
                       restore a dump + copy missing music files back

Layout of BACKUP_DIR (a volume, ideally on another disk / a NAS):

  db/soundable-YYYYmmdd-HHMMSS.sql.gz   plain SQL dumps, newest BACKUP_KEEP kept
  music/<track-id>.<ext>                mirror of MUSIC_DIR (files are immutable, so only
                                        new ones are copied; files of deleted tracks stay)
  last-backup.json                      status of the last run
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.engine import make_url

from app.config import Settings, get_settings
from app.library import TMP_DIRNAME

log = logging.getLogger("app.backup")

DUMP_PREFIX = "soundable-"
DUMP_SUFFIX = ".sql.gz"
# pg_dump 17 emits this setting, which PostgreSQL 16 doesn't know: skipped on restore.
_PG17_ONLY = (b"SET transaction_timeout",)


class BackupError(RuntimeError):
    pass


def _pg_env(s: Settings) -> dict[str, str]:
    """libpq environment for pg_dump/psql, from the same URL the app connects with."""
    url = make_url(s.sqlalchemy_url)
    return {
        **os.environ,
        "PGHOST": url.host or "localhost",
        "PGPORT": str(url.port or 5432),
        "PGUSER": url.username or "",
        "PGPASSWORD": url.password or "",
        "PGDATABASE": url.database or "",
    }


def db_dir(s: Settings) -> Path:
    d = s.backup_dir / "db"
    d.mkdir(parents=True, exist_ok=True)
    return d


def mirror_dir(s: Settings) -> Path:
    d = s.backup_dir / "music"
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_dumps(s: Settings) -> list[Path]:
    return sorted(db_dir(s).glob(f"{DUMP_PREFIX}*{DUMP_SUFFIX}"))


def dump_database(s: Settings) -> Path:
    stamp = f"{datetime.now(UTC):%Y%m%d-%H%M%S}"
    final = db_dir(s) / f"{DUMP_PREFIX}{stamp}{DUMP_SUFFIX}"
    n = 2
    while final.exists():  # several backups within the same second
        final = db_dir(s) / f"{DUMP_PREFIX}{stamp}-{n}{DUMP_SUFFIX}"
        n += 1
    tmp = final.with_suffix(".partial")
    proc = subprocess.Popen(
        ["pg_dump", "--clean", "--if-exists", "--no-owner", "--no-privileges"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_pg_env(s),
    )
    assert proc.stdout is not None and proc.stderr is not None
    # Drain stderr in the background so a chatty pg_dump can't dead-lock on a full pipe.
    err_chunks: list[bytes] = []
    t = threading.Thread(target=lambda: err_chunks.append(proc.stderr.read()), daemon=True)  # type: ignore[union-attr]
    t.start()
    try:
        with gzip.open(tmp, "wb", compresslevel=6) as out:
            shutil.copyfileobj(proc.stdout, out, 1024 * 1024)
        code = proc.wait()
        t.join(5)
        if code != 0:
            raise BackupError(
                f"pg_dump failed: {b''.join(err_chunks).decode(errors='replace')[-500:]}"
            )
        os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    return final


def mirror_music(s: Settings) -> tuple[int, int]:
    """Copy new/changed files to the mirror. Returns (copied, total files)."""
    dest = mirror_dir(s)
    copied = total = 0
    for src in s.music_dir.iterdir():
        if not src.is_file() or src.name.startswith(".") or src.parent.name == TMP_DIRNAME:
            continue
        total += 1
        target = dest / src.name
        if target.exists() and target.stat().st_size == src.stat().st_size:
            continue
        part = target.with_name(target.name + ".partial")
        shutil.copy2(src, part)
        os.replace(part, target)
        copied += 1
    return copied, total


def prune(s: Settings) -> list[Path]:
    dumps = list_dumps(s)
    old = dumps[: max(0, len(dumps) - s.backup_keep)]
    for p in old:
        p.unlink(missing_ok=True)
    return old


def run_backup(s: Settings | None = None) -> dict[str, object]:
    s = s or get_settings()
    started = time.monotonic()
    status: dict[str, object] = {"started_at": datetime.now(UTC).isoformat()}
    try:
        dump = dump_database(s)
        copied, total = mirror_music(s)
        removed = prune(s)
        status.update(
            ok=True,
            dump=dump.name,
            dump_bytes=dump.stat().st_size,
            music_files=total,
            music_copied=copied,
            pruned=[p.name for p in removed],
        )
        log.info(
            "backup ok: %s (%.1f MB), music %d files (%d new), pruned %d old dump(s)",
            dump.name,
            dump.stat().st_size / 2**20,
            total,
            copied,
            len(removed),
        )
    except Exception as e:
        status.update(ok=False, error=str(e))
        log.error("backup FAILED: %s", e)
    status["seconds"] = round(time.monotonic() - started, 1)
    status["finished_at"] = datetime.now(UTC).isoformat()
    with open(s.backup_dir / "last-backup.json", "w", encoding="utf-8") as f:
        json.dump(status, f, indent=2)
    return status


def _other_connections(s: Settings) -> int:
    out = subprocess.run(
        [
            "psql",
            "-tA",
            "-c",
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE datname = current_database() AND pid <> pg_backend_pid()",
        ],
        capture_output=True,
        env=_pg_env(s),
        check=True,
    )
    return int(out.stdout.strip() or 0)


def restore(name: str = "latest", *, force: bool = False, s: Settings | None = None) -> None:
    s = s or get_settings()
    dumps = list_dumps(s)
    if not dumps:
        raise BackupError(f"no dumps in {db_dir(s)}")
    dump = dumps[-1] if name == "latest" else db_dir(s) / name
    if not dump.is_file():
        raise BackupError(f"no such dump: {name} (see `list`)")
    if not force and (n := _other_connections(s)):
        raise BackupError(
            f"{n} other connection(s) to the database: stop the api and worker containers "
            "first (or pass --force)."
        )
    log.info("restoring database from %s", dump.name)
    proc = subprocess.Popen(
        ["psql", "-q", "-v", "ON_ERROR_STOP=1", "--single-transaction"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        env=_pg_env(s),
    )
    assert proc.stdin is not None
    try:
        with gzip.open(dump, "rb") as f:
            for line in f:
                if not line.startswith(_PG17_ONLY):
                    proc.stdin.write(line)
        proc.stdin.close()
    except BrokenPipeError:
        pass
    err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
    if proc.wait() != 0:
        raise BackupError(f"psql failed: {err[-800:]}")

    restored = 0
    s.music_dir.mkdir(parents=True, exist_ok=True)
    for src in mirror_dir(s).iterdir():
        if not src.is_file() or src.name.endswith(".partial"):
            continue
        target = s.music_dir / src.name
        if not target.exists():
            part = target.with_name(target.name + ".partial")
            shutil.copy2(src, part)
            os.replace(part, target)
            restored += 1
    log.info("restore done: database from %s, %d music file(s) copied back", dump.name, restored)


def loop() -> None:
    s = get_settings()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    interval = max(1, s.backup_interval_hours) * 3600
    log.info(
        "backup service: every %d h into %s, keeping %d dumps",
        interval // 3600,
        s.backup_dir,
        s.backup_keep,
    )
    # First run shortly after start (lets the database come up), then on schedule.
    if stop.wait(60):
        return
    while not stop.is_set():
        run_backup(s)
        stop.wait(interval)


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level=get_settings().log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cmd = argv[0] if argv else "run"
    s = get_settings()
    try:
        if cmd == "loop":
            loop()
        elif cmd == "run":
            return 0 if run_backup(s).get("ok") else 1
        elif cmd == "list":
            for p in list_dumps(s):
                print(f"{p.name}  {p.stat().st_size / 2**20:8.1f} MB")
            print(f"music mirror: {sum(1 for _ in mirror_dir(s).iterdir())} files")
        elif cmd == "restore":
            args = [a for a in argv[1:] if not a.startswith("--")]
            restore(args[0] if args else "latest", force="--force" in argv)
        else:
            print(__doc__)
            return 2
    except BackupError as e:
        log.error("%s", e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
