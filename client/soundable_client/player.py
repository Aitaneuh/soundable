"""Playback queue on top of mpv.

The queue *is* mpv's playlist: mpv advances to the next track by itself (even while the
UI is busy) and Windows media keys act on it directly. We keep a parallel list of Track
objects and only ever mutate the playlist through this class, so indexes stay in sync.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from soundable_client.api import Track
from soundable_client.mpv import Mpv, MpvError, escape_option_value

log = logging.getLogger(__name__)

REPEAT_MODES = ("off", "all", "one")
NORMALIZE_FILTER = "lavfi=[loudnorm=I=-16:TP=-1.5:LRA=11]"
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
    context: str | None = None  # e.g. the playlist name the queue came from
    shuffle: bool = False
    repeat: str = "off"  # off | all | one

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
        self._original: list[Track] = []  # queue order before shuffling
        self.normalize = False
        self.audio_device = "auto"
        self._listeners: list[Callable[[PlayerState], None]] = []
        self._played_listeners: list[Callable[[Track], None]] = []
        self._listened = 0.0  # seconds actually heard of the current track
        self._reported = False
        self._error_listeners: list[Callable[[str], None]] = []
        mpv.on_event(self._on_event)

    def on_change(self, cb: Callable[[PlayerState], None]) -> None:
        self._listeners.append(cb)

    def on_played(self, cb: Callable[[Track], None]) -> None:
        """Called once per listen, after half the track (or 4 minutes), like scrobblers."""
        self._played_listeners.append(cb)

    def _track_listening(self, new_pos: float) -> None:
        s = self.state
        delta = new_pos - s.position
        if self._reported and new_pos < 2 and delta < -5:
            # the same track started over (repeat one): a new listen
            self._listened, self._reported = 0.0, False
        if not s.paused and 0 < delta < 2:  # ignore seeks
            self._listened += delta
        cur = s.current
        if cur is None or self._reported:
            return
        duration = s.duration or cur.duration
        threshold = min(max(duration * 0.5, 10.0), 240.0) if duration > 0 else 240.0
        if self._listened >= threshold:
            self._reported = True
            for cb in self._played_listeners:
                cb(cur)

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
                new_pos = float(data or 0)
                self._track_listening(new_pos)
                s.position = new_pos
            elif prop == "duration":
                s.duration = float(data or 0)
            elif prop == "pause":
                s.paused = bool(data)
            elif prop == "volume":
                s.volume = round(float(data or 0))
            elif prop == "playlist-pos":
                s.index = int(data) if data is not None else -1
                s.position = 0.0
                self._listened, self._reported = 0.0, False
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
            self._original = []
            await self.start()
            await self._apply_repeat()
            await self._apply_audio_settings()

    async def _load(self, tracks: list[Track]) -> None:
        """Replace mpv's playlist with `tracks` without starting playback."""
        await self.mpv.command("stop")  # also clears the playlist
        self.state.queue = []
        for t in tracks:
            await self._append(t)
            self.state.queue.append(t)

    async def play_list(
        self, tracks: list[Track], start: int = 0, context: str | None = None
    ) -> None:
        """Replace the queue with `tracks` and start at `start`. In shuffle mode the
        chosen track plays first and the rest follow in random order."""
        await self.ensure_running()
        self.state.context = context
        self._original = list(tracks)
        order = list(tracks)
        if self.state.shuffle and len(tracks) > 1:
            rest = tracks[:start] + tracks[start + 1 :]
            random.shuffle(rest)
            order, start = [tracks[start], *rest], 0
        await self._load(order)
        await self.mpv.command("playlist-play-index", start)
        await self.mpv.set("pause", False)

    async def enqueue(self, tracks: list[Track]) -> None:
        await self.ensure_running()
        for t in tracks:
            await self._append(t, "append-play")
            self.state.queue.append(t)
            self._original.append(t)
        self._notify()

    async def play_index(self, i: int) -> None:
        if 0 <= i < len(self.state.queue):
            await self.mpv.command("playlist-play-index", i)
            await self.mpv.set("pause", False)

    async def remove(self, i: int) -> Track:
        track = self.state.queue[i]
        await self.mpv.command("playlist-remove", i)
        del self.state.queue[i]
        with contextlib.suppress(ValueError):
            self._original.remove(track)
        self._notify()
        return track

    async def clear(self) -> None:
        if self.mpv.running:
            await self.mpv.command("stop")
        self.state.queue = []
        self._original = []
        self.state.index = -1
        self.state.context = None
        self._notify()

    # --- shuffle / repeat -------------------------------------------------------------

    async def set_shuffle(self, on: bool) -> None:
        """Toggle shuffle without interrupting the current track: on = the rest of the
        queue is randomised; off = the original order comes back."""
        s = self.state
        if on == s.shuffle:
            return
        s.shuffle = on
        if len(s.queue) < 2 or not self.mpv.running:
            self._notify()
            return
        cur = s.current
        if on:
            if cur is None:
                order = list(s.queue)
                random.shuffle(order)
            else:
                rest = s.queue[: s.index] + s.queue[s.index + 1 :]
                random.shuffle(rest)
                order = [cur, *rest]
            new_index = 0
        else:
            order = list(self._original) or list(s.queue)
            new_index = order.index(cur) if cur is not None and cur in order else 0
        await self._reorder(order, new_index if cur is not None else -1)
        self._notify()

    async def _reorder(self, order: list[Track], current_at: int) -> None:
        if current_at < 0:  # nothing playing: just rebuild the playlist
            await self._load(order)
            return
        # mpv keeps the playing entry on playlist-clear: rebuild around it.
        await self.mpv.command("playlist-clear")
        for t in order[current_at + 1 :]:
            await self._append(t)
        for k, t in enumerate(order[:current_at]):
            await self.mpv.command("loadfile", self._url(t), "insert-at", k, self._file_options(t))
        self.state.queue = list(order)
        self.state.index = current_at

    async def set_repeat(self, mode: str) -> None:
        if mode not in REPEAT_MODES:
            raise ValueError(mode)
        self.state.repeat = mode
        await self._apply_repeat()
        self._notify()

    async def cycle_repeat(self) -> str:
        nxt = REPEAT_MODES[(REPEAT_MODES.index(self.state.repeat) + 1) % len(REPEAT_MODES)]
        await self.set_repeat(nxt)
        return nxt

    async def _apply_repeat(self) -> None:
        if self.mpv.running:
            await self.mpv.set("loop-file", "inf" if self.state.repeat == "one" else "no")
            await self.mpv.set("loop-playlist", "inf" if self.state.repeat == "all" else "no")

    # --- transport --------------------------------------------------------------------

    async def toggle_pause(self) -> None:
        if self.mpv.running and self.state.current:
            await self.mpv.command("cycle", "pause")

    async def set_pause(self, paused: bool) -> None:
        if self.mpv.running:
            await self.mpv.set("pause", paused)

    async def next(self) -> bool:
        s = self.state
        if s.index + 1 < len(s.queue):
            await self.mpv.command("playlist-next", "force")
            return True
        if s.repeat == "all" and s.queue:
            await self.play_index(0)
            return True
        return False

    async def prev(self) -> None:
        # Like most players: restart the track unless we're at its very beginning.
        s = self.state
        if s.position > 3:
            await self.seek_to(0)
        elif s.index > 0:
            await self.mpv.command("playlist-prev", "force")
        elif s.repeat == "all" and len(s.queue) > 1:
            await self.play_index(len(s.queue) - 1)
        else:
            await self.seek_to(0)

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

    # --- audio settings -----------------------------------------------------------------

    async def set_normalize(self, on: bool) -> None:
        """EBU R128 loudness normalisation, so quiet and loud imports play at similar levels."""
        self.normalize = on
        if self.mpv.running:
            await self.mpv.set("af", NORMALIZE_FILTER if on else "")

    async def audio_devices(self) -> list[tuple[str, str]]:
        """(name, description) pairs from mpv, 'auto' first."""
        await self.ensure_running()
        devices = await self.mpv.get("audio-device-list") or []
        return [(d.get("name", ""), d.get("description") or d.get("name", "")) for d in devices]

    async def set_audio_device(self, name: str) -> None:
        self.audio_device = name or "auto"
        if self.mpv.running:
            await self.mpv.set("audio-device", self.audio_device)

    async def _apply_audio_settings(self) -> None:
        if self.normalize:
            await self.mpv.set("af", NORMALIZE_FILTER)
        if self.audio_device != "auto":
            with contextlib.suppress(MpvError):
                await self.mpv.set("audio-device", self.audio_device)

    # --- session restore --------------------------------------------------------------

    def snapshot(self) -> tuple[list[str], list[str], int, float, str | None]:
        s = self.state
        return (
            [t.id for t in s.queue],
            [t.id for t in self._original],
            s.index,
            s.position,
            s.context,
        )

    async def restore(
        self,
        queue: list[Track],
        original: list[Track],
        index: int,
        position: float,
        context: str | None,
    ) -> None:
        """Load a saved queue, paused at `position` of track `index`."""
        if not queue:
            return
        await self.ensure_running()
        index = max(0, min(index, len(queue) - 1))
        await self.mpv.set("pause", True)
        await self._load(queue)
        self._original = list(original) or list(queue)
        self.state.context = context
        await self.mpv.command("playlist-play-index", index)
        if position > 1:
            # wait for the file to open before seeking
            for _ in range(50):
                if self.state.duration > 0 and self.state.index == index:
                    break
                await asyncio.sleep(0.1)
            if position < self.state.duration - 2:  # never seek past the end
                await self.seek_to(position)
        self._notify()

    async def shutdown(self) -> None:
        await self.mpv.quit()
