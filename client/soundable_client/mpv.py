"""Minimal async controller for an mpv process over its JSON IPC (named pipe on Windows,
Unix socket elsewhere).

Why not python-mpv: it needs the libmpv shared library (mpv-2.dll / libmpv.so), which is
awkward to install, especially on Windows. Driving the regular `mpv` binary over IPC
only needs mpv itself, and the player keeps running even if the UI is busy.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from soundable_client.config import state_dir

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

WINDOWS_CANDIDATES = [
    r"C:\Program Files\MPV Player\mpv.exe",
    r"C:\Program Files\mpv\mpv.exe",
    os.path.expandvars(r"%LOCALAPPDATA%\Programs\mpv\mpv.exe"),
    os.path.expandvars(r"%USERPROFILE%\scoop\apps\mpv\current\mpv.exe"),
    r"C:\ProgramData\chocolatey\bin\mpv.exe",
]


class MpvError(Exception):
    pass


def find_mpv(configured: str = "") -> str | None:
    if configured and Path(configured).is_file():
        return configured
    found = shutil.which("mpv")
    if found:
        return found
    if IS_WINDOWS:
        for c in WINDOWS_CANDIDATES:
            if Path(c).is_file():
                return c
    return None


def install_hint() -> str:
    if IS_WINDOWS:
        return "Install it with:  winget install shinchiro.mpv   (then restart the terminal)"
    if sys.platform == "darwin":
        return "Install it with:  brew install mpv"
    return "Install it with your package manager, e.g.  sudo apt install mpv"


def escape_option_value(value: str) -> str:
    """mpv's %<bytes>% quoting for per-file option values containing , = or spaces."""
    return f"%{len(value.encode())}%{value}"


EventHandler = Callable[[dict[str, Any]], None]


class Mpv:
    def __init__(self, binary: str, *, volume: int = 70) -> None:
        self.binary = binary
        self.initial_volume = volume
        self.proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._read_task: asyncio.Task[None] | None = None
        self._handlers: list[EventHandler] = []
        self._observe_ids = itertools.count(1)
        self.closing = False
        if IS_WINDOWS:
            self.ipc_path = rf"\\.\pipe\soundable-mpv-{os.getpid()}"
        else:
            self.ipc_path = str(state_dir() / f"mpv-{os.getpid()}.sock")

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.returncode is None and self._writer is not None

    def on_event(self, handler: EventHandler) -> None:
        self._handlers.append(handler)

    async def start(self) -> None:
        self.closing = False
        args = [
            self.binary,
            "--idle=yes",
            "--no-video",
            "--force-window=no",
            "--no-terminal",
            "--no-config",  # predictable behaviour regardless of the user's mpv.conf
            "--audio-display=no",
            f"--input-ipc-server={self.ipc_path}",
            f"--volume={self.initial_volume}",
            "--volume-max=100",
            "--cache=yes",
            "--demuxer-max-bytes=64MiB",
            "--title=soundable",
            "--keep-open=no",
            "--gapless-audio=weak",
        ]
        if IS_WINDOWS:
            # Lets Windows media keys / the volume overlay control playback (mpv >= 0.40).
            args.append("--media-controls=yes")
        kwargs: dict[str, Any] = {}
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            Path(self.ipc_path).unlink(missing_ok=True)
            kwargs["start_new_session"] = True  # don't receive the terminal's Ctrl+C
        self.proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            **kwargs,
        )
        await self._connect()
        self._read_task = asyncio.create_task(self._read_loop())

    async def _connect(self, timeout: float = 8.0) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last: Exception | None = None
        while loop.time() < deadline:
            if self.proc and self.proc.returncode is not None:
                raise MpvError(f"mpv exited immediately (code {self.proc.returncode})")
            try:
                if IS_WINDOWS:
                    reader = asyncio.StreamReader(limit=2**20)
                    proto = asyncio.StreamReaderProtocol(reader)
                    transport, _ = await loop.create_pipe_connection(  # type: ignore[attr-defined]
                        lambda proto=proto: proto, self.ipc_path
                    )
                    writer = asyncio.StreamWriter(transport, proto, reader, loop)
                else:
                    reader, writer = await asyncio.open_unix_connection(self.ipc_path, limit=2**20)
                self._reader, self._writer = reader, writer
                return
            except (FileNotFoundError, ConnectionRefusedError, OSError) as e:
                last = e
                await asyncio.sleep(0.1)
        raise MpvError(f"could not connect to mpv IPC: {last}")

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while line := await self._reader.readline():
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "request_id" in msg and msg["request_id"] in self._pending:
                    fut = self._pending.pop(msg["request_id"])
                    if not fut.done():
                        if msg.get("error") == "success":
                            fut.set_result(msg.get("data"))
                        else:
                            fut.set_exception(MpvError(str(msg.get("error"))))
                elif "event" in msg:
                    for h in self._handlers:
                        try:
                            h(msg)
                        except Exception:
                            log.exception("mpv event handler failed")
        except (ConnectionError, OSError):
            pass
        finally:
            if self._writer is not None:
                self._writer.close()
            self._writer = None
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(MpvError("mpv connection closed"))
            self._pending.clear()
            for h in self._handlers:
                with contextlib.suppress(Exception):
                    h({"event": "ipc-closed"})

    async def command(self, *args: Any, timeout: float = 5.0) -> Any:
        if not self._writer:
            raise MpvError("mpv is not running")
        rid = next(self._ids)
        fut: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        payload = json.dumps({"command": list(args), "request_id": rid}) + "\n"
        self._writer.write(payload.encode())
        await self._writer.drain()
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(rid, None)

    async def get(self, name: str) -> Any:
        return await self.command("get_property", name)

    async def set(self, name: str, value: Any) -> None:
        await self.command("set_property", name, value)

    async def observe(self, name: str) -> None:
        await self.command("observe_property", next(self._observe_ids), name)

    async def quit(self) -> None:
        self.closing = True
        if self.running:
            with contextlib.suppress(Exception):
                await self.command("quit", timeout=1.0)
        if self.proc and self.proc.returncode is None:
            try:
                await asyncio.wait_for(self.proc.wait(), 2.0)
            except TimeoutError:
                self.proc.kill()
        if self._writer is not None:
            self._writer.close()
        if self._read_task:
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._read_task
        if not IS_WINDOWS:
            Path(self.ipc_path).unlink(missing_ok=True)
