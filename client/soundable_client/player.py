"""Playback queue on top of mpv.

The queue *is* mpv's playlist: mpv advances to the next track by itself (even while the
UI is busy) and Windows media keys act on it directly. We keep a parallel list of Track
objects and only ever mutate the playlist through this class, so indexes stay in sync.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from soundable_client.api import Track
from soundable_client.mpv import Mpv, MpvError, escape_option_value

log = logging.getLogger(__name__)

OBSERVED = ("time-pos", "duration", "pause", "volume", "playlist-pos", "idle-active")


@dataclass
class PlayerState:
    queue: list[Track] = field(default_factory=list)
    index: int = -1  # position in queue, -1 = nothing playing
    paused: bool = False
    position: float = 0.0
    duration: float = 0.0
    volume: int = 70
    idle: bool = True
    error: str | None = None

    @property
    def current(self) -> Track | None:
        if 0 <= self.index < len(self.queue):
            return self.queue[self.index]
        return None


class Player:
    def __init__(self, mpv: Mpv, base_url: str, token: str | None) -> None:
        self.mpv = mpv
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.state = PlayerState(volume=mpv.initial_volume)
        self._listeners: list[Callable[[PlayerState], None]] = []
        self._error_listeners: list[Callable[[str], None]] = []
        mpv.on_event(self._on_event)

    def on_change(self, cb: Callable[[PlayerState], None]) -> None:
        self._listeners.append(cb)

    def on_error(self, cb: Callable[[str], None]) -> None:
        self._error_listeners.append(cb)

    def _notify(self) -> None:
        for cb in self._listeners:
            cb(self.state)

    async def start(self) -> None:
        await self.mpv.start()
        for prop in OBSERVED:
            await self.mpv.observe(prop)
        await self.set_token(self.token)

    async def set_token(self, token: str | None) -> None:
        """mpv sends this header on every request (incl. Range requests when seeking).
        Set over IPC, so the token never appears on a command line."""
        self.token = token
        if self.mpv.running:
            headers = [f"Authorization: Bearer {token}"] if token else []
            await self.mpv.set("http-header-fields", headers)

    def _url(self, track: Track) -> str:
        return f"{self.base_url}/api/tracks/{track.id}/stream"

    def _file_options(self, track: Track) -> str:
        return "force-media-title=" + escape_option_value(f"{track.title} — {track.artist}")

    async def _append(self, track: Track, flag: str = "append") -> None:
        # loadfile <url> <flags> <index> <options>; index -1 = end (mpv >= 0.38)
        await self.mpv.command("loadfile", self._url(track), flag, -1, self._file_options(track))

    # --- events ---------------------------------------------------------------------------

    def _on_event(self, ev: dict[str, Any]) -> None:
        name = ev.get("event")
        s = self.state
        if name == "property-change":
            prop, data = ev.get("name"), ev.get("data")
            if prop == "time-pos":
                s.position = float(data or 0)
            elif prop == "duration":
                s.duration = float(data or 0)
            elif prop == "pause":
                s.paused = bool(data)
            elif prop == "volume":
                s.volume = round(float(data or 0))
            elif prop == "playlist-pos":
                s.index = int(data) if data is not None else -1
                s.position = 0.0
            elif prop == "idle-active":
                s.idle = bool(data)
                if s.idle:
                    s.position = 0.0
            self._notify()
        elif name == "end-file" and ev.get("reason") == "error":
            msg = ev.get("file_error") or "playback error"
            s.error = msg
            for cb in self._error_listeners:
                cb(msg)
        elif name == "ipc-closed":
            s.idle = True
            s.index = -1
            self._notify()
            if self.mpv.closing:
                return
            for cb in self._error_listeners:
                cb("mpv stopped unexpectedly; it will be restarted on the next play")

    # --- controls -------------------------------------------------------------------------

    async def ensure_running(self) -> None:
        if not self.mpv.running:
            self.state.queue = []
            await self.start()

    async def play_list(self, tracks: list[Track], start: int = 0) -> None:
        """Replace the queue with `tracks` and start at `start`."""
        await self.ensure_running()
        await self.mpv.command("stop")  # also clears the playlist
        self.state.queue = []
        for t in tracks:
            await self._append(t)
            self.state.queue.append(t)
        await self.mpv.command("playlist-play-index", start)
        await self.mpv.set("pause", False)

    async def enqueue(self, tracks: list[Track]) -> None:
        await self.ensure_running()
        for t in tracks:
            await self._append(t, "append-play")
            self.state.queue.append(t)
        self._notify()

    async def play_index(self, i: int) -> None:
        if 0 <= i < len(self.state.queue):
            await self.mpv.command("playlist-play-index", i)
            await self.mpv.set("pause", False)

    async def remove(self, i: int) -> Track:
        track = self.state.queue[i]
        await self.mpv.command("playlist-remove", i)
        del self.state.queue[i]
        self._notify()
        return track

    async def clear(self) -> None:
        if self.mpv.running:
            await self.mpv.command("stop")
        self.state.queue = []
        self.state.index = -1
        self._notify()

    async def toggle_pause(self) -> None:
        if self.mpv.running and self.state.current:
            await self.mpv.command("cycle", "pause")

    async def set_pause(self, paused: bool) -> None:
        if self.mpv.running:
            await self.mpv.set("pause", paused)

    async def next(self) -> bool:
        if self.state.index + 1 < len(self.state.queue):
            await self.mpv.command("playlist-next", "force")
            return True
        return False

    async def prev(self) -> None:
        # Like most players: restart the track unless we're at its very beginning.
        if self.state.position > 3 or self.state.index <= 0:
            await self.seek_to(0)
        else:
            await self.mpv.command("playlist-prev", "force")

    async def seek(self, seconds: float) -> None:
        if self.state.current:
            with contextlib.suppress(MpvError, TimeoutError):  # e.g. nothing loaded yet
                await self.mpv.command("seek", seconds, "relative")

    async def seek_to(self, seconds: float) -> None:
        if self.state.current:
            with contextlib.suppress(MpvError, TimeoutError):  # e.g. nothing loaded yet
                await self.mpv.command("seek", seconds, "absolute")

    async def change_volume(self, delta: int) -> int:
        return await self.set_volume(self.state.volume + delta)

    async def set_volume(self, value: int) -> int:
        value = max(0, min(100, value))
        await self.ensure_running()
        await self.mpv.set("volume", value)
        self.state.volume = value
        self._notify()
        return value

    async def shutdown(self) -> None:
        await self.mpv.quit()
