"""The soundable Textual app: Claude-Code-style log + prompt, plus listening/browse modes."""

from __future__ import annotations

import asyncio
import contextlib
import random
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from rich import box
from rich.console import RenderableType
from rich.table import Table
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.screen import ModalScreen
from textual.widgets import ContentSwitcher, Input, RichLog, Static

from soundable_client import __version__, keys
from soundable_client.api import Api, ApiError, AuthError, Job, NetworkError, Playlist, Track
from soundable_client.commands import match_commands
from soundable_client.config import (
    Config,
    SessionState,
    delete_token,
    load_token,
    save_token,
)
from soundable_client.formatting import (
    DEFAULT_ACCENT,
    accent,
    fmt_size,
    fmt_time,
    fold,
    set_accent,
    track_table,
)
from soundable_client.inputs import AUDIO_EXTENSIONS, classify, expand_audio_paths, split_paths
from soundable_client.mpv import Mpv, MpvError, find_mpv, install_hint
from soundable_client.player import Player, PlayerState
from soundable_client.screens import (
    NEW_PLAYLIST,
    ConfirmScreen,
    EditTrackScreen,
    LoginResult,
    LoginScreen,
    NameScreen,
    PlaylistPickerScreen,
)
from soundable_client.views import BrowseView, ListenView, help_text
from soundable_client.widgets import CommandMenu, NowPlaying, PromptInput, StatusHeader, TaskList

MODE_VIEWS = {"prompt": "log", "listen": "listen", "browse": "browse"}


def mode_hints(mode: str) -> str:
    k = keys.show
    if mode == "listen":
        return (
            f"{k('listen.prev')}/{k('listen.next')} prev/next · "
            f"{k('listen.vol_up')}/{k('listen.vol_down')} volume · {k('listen.pause')} pause · "
            f"{k('listen.back')}/{k('listen.fwd')} seek · {k('listen.shuffle')} shuffle · "
            f"{k('listen.repeat')} repeat · Esc prompt · {k('app.browse')} browse"
        )
    if mode == "browse":
        return (
            "Tab/Shift+Tab switch Library · Playlists · Queue · Enter play · "
            f"{k('browse.pause')} pause · Esc back/prompt · {k('app.settings')} settings"
        )
    return ""


class SoundableApp(App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "soundable"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        keys.binding("app.browse", "toggle_browse", priority=True),
        keys.binding("app.shuffle", "player_action('shuffle')", priority=True),
        keys.binding("app.repeat", "player_action('repeat')", priority=True),
        keys.binding("app.settings", "open_settings", priority=True),
        keys.binding("app.quit", "quit", priority=True),
    ]

    def __init__(self, config: Config, *, force_login: bool = False) -> None:
        super().__init__()
        self.config = config
        self.force_login = force_login
        self.api: Api | None = None
        self.player: Player | None = None
        self.mpv_path = find_mpv(config.mpv_path)
        self.mode = "prompt"
        self.last: list[Track] = []
        self.last_kind = "tracks"  # or "queue": what "#n" refers to
        self.online = False
        self._login_open = False
        self._tracked_jobs: set[str] = set()
        self._restored = False
        self._started_mode = False
        self.last_playlists: list[Playlist] = []
        self.last_playlist: Playlist | None = None

    # --- layout -----------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield StatusHeader(id="header")
        with ContentSwitcher(initial="log", id="main"):
            yield RichLog(id="log", wrap=True, markup=False, auto_scroll=True, min_width=40)
            yield ListenView(id="listen")
            yield BrowseView(id="browse")
        yield TaskList(id="tasks")
        yield NowPlaying(id="np")
        yield CommandMenu(id="menu")
        yield PromptInput(self.config.history, id="prompt")
        yield Static("", id="hints")

    @property
    def log_view(self) -> RichLog:
        return self.query_one("#log", RichLog)

    @property
    def header(self) -> StatusHeader:
        return self.query_one(StatusHeader)

    @property
    def tasks(self) -> TaskList:
        return self.query_one(TaskList)

    @property
    def prompt(self) -> PromptInput:
        return self.query_one(PromptInput)

    # --- output helpers ---------------------------------------------------------------

    def _write_text(self, t: Text) -> None:
        """Wrap text lines at the log's real width (RichLog otherwise measures against the
        console width, which can be narrower than the widget)."""
        if self.log_view.scrollable_content_region.width >= 20:
            self.log_view.write(t, expand=True)
        else:  # log hidden (browse/listen mode): it reports a zero width
            self.log_view.write(t, width=max(self.size.width - 4, 40))

    def say(self, message: str | Text, kind: str = "info") -> None:
        color = {"info": "white", "ok": "green", "warn": "yellow", "error": "red"}[kind]
        t = Text()
        t.append("⏺ ", style=color if kind != "info" else accent())
        t.append_text(message if isinstance(message, Text) else Text(message))
        self._write_text(t)

    def show(self, renderable: RenderableType) -> None:
        self.log_view.write(renderable)

    def echo(self, line: str) -> None:
        t = Text()
        t.append("› ", style="bold dim")
        t.append(line, style="bold")
        self.log_view.write(Text(""))
        self._write_text(t)

    def show_tracks(self, tracks: list[Track], title: str | None = None) -> None:
        self.last, self.last_kind = tracks, "tracks"
        self.show(track_table(tracks, title=title))

    # --- lifecycle --------------------------------------------------------------------

    def get_css_variables(self) -> dict[str, str]:
        # Called by Textual's constructor too, before self.config exists.
        config = getattr(self, "config", None)
        color = config.accent if config else DEFAULT_ACCENT
        return {**super().get_css_variables(), "soundable-accent": color}

    def _apply_appearance(self) -> None:
        if self.config.theme in self.available_themes:
            self.theme = self.config.theme
        set_accent(self.config.accent)
        self.refresh_css()

    def _apply_keymap(self) -> None:
        keys.set_overrides(self.config.keymap)
        self.set_keymap(dict(self.config.keymap))

    def on_mount(self) -> None:
        self._apply_appearance()
        self._apply_keymap()
        self.header.server = self.config.server_url
        self.prompt.focus()
        welcome = Text()
        welcome.append("✻ ", style=f"bold {accent()}")
        welcome.append(f"soundable {__version__}", style="bold")
        welcome.append("  ·  /help for commands  ·  Esc listening mode  ·  Ctrl+B browse")
        self._write_text(welcome)
        self.set_interval(0.25, self._refresh_player_views)
        self.set_interval(15, self._health_tick)
        self.startup()

    @work(exclusive=True, group="startup")
    async def startup(self) -> None:
        await self._start_player()
        token = load_token(self.config.server_url) if self.config.server_url else None
        if self.force_login or not self.config.server_url or not token:
            self.header.status = "logged-out"
            self.open_login(can_cancel=bool(token))
            return
        await self._connect(self.config.server_url, token)

    async def _start_player(self) -> None:
        np = self.query_one(NowPlaying)
        if not self.mpv_path:
            np.available = False
            self.say(f"mpv was not found, so playback is disabled. {install_hint()}", "warn")
            return
        # The player is (re)bound to the server once we're logged in.
        self.player = Player(Mpv(self.mpv_path, volume=self.config.volume), "", None)
        self.player.on_error(lambda msg: self.say(f"Playback: {msg}", "error"))
        self.player.state.shuffle = self.config.shuffle
        self.player.normalize = self.config.normalize
        self.player.audio_device = self.config.audio_device
        repeat = self.config.repeat if self.config.repeat in ("off", "all", "one") else "off"
        try:
            await self.player.start()
            await self.player.set_repeat(repeat)
        except (MpvError, OSError) as e:
            self.say(f"Could not start mpv ({e}). Playback disabled.", "error")
            np.available = False
            self.player = None

    async def _connect(self, url: str, token: str) -> None:
        if self.api:
            await self.api.aclose()
        self.api = Api(url, token)
        self.header.server = url
        if self.player:
            self.player.base_url = url
            await self.player.set_token(token)
        try:
            me = await self.api.me()
        except AuthError:
            self.header.status = "logged-out"
            self.say("Your session is no longer valid. Please log in again.", "warn")
            self.open_login()
            return
        except NetworkError as e:
            self.header.status = "offline"
            self.say(f"Server unreachable ({e}). I'll keep retrying.", "warn")
            return
        self.online = True
        self.header.status = "online"
        _, total = await self.api.tracks(limit=1)
        self.say(f"Connected to {url} as '{me['name']}' · {total} tracks in the library.", "ok")
        for job in await self.api.jobs(status=["queued", "running"], limit=50):
            self.track_job(job)
        await self._restore_session()
        if self.config.start_mode in ("listen", "browse") and not self._started_mode:
            self._started_mode = True
            self.set_mode(self.config.start_mode)

    async def _restore_session(self) -> None:
        if not (self.config.restore_queue and self.player and self.api) or self._restored:
            return
        self._restored = True
        st = SessionState.load()
        if st.server_url != self.api.base_url or not st.queue:
            return
        try:
            by_id = {t.id: t for t in await self.api.all_tracks()}
        except ApiError:
            return
        queue = [by_id[i] for i in st.queue if i in by_id]
        original = [by_id[i] for i in st.original if i in by_id]
        if not queue:
            return
        current = st.queue[st.index] if 0 <= st.index < len(st.queue) else None
        index = next((k for k, t in enumerate(queue) if t.id == current), 0)
        await self.player.restore(queue, original, index, st.position, st.context)
        t = queue[index]
        self.say(
            Text.assemble(
                "Resumed your queue (paused): ",
                (t.title, "bold"),
                " — ",
                (t.artist, "cyan"),
                (f" at {fmt_time(st.position)} · space/Esc to listen", "dim"),
            )
        )

    def _save_session(self) -> None:
        if not (self.player and self.api):
            return
        queue, original, index, position, context = self.player.snapshot()
        with contextlib.suppress(OSError):
            SessionState(self.api.base_url, queue, original, index, position, context).save()

    def open_login(self, *, can_cancel: bool = True) -> None:
        if self._login_open:
            return
        self._login_open = True

        def done(result: LoginResult | None) -> None:
            self._login_open = False
            if result is None:
                if not self.api:
                    self.say("Not logged in. Use /login when you're ready.", "warn")
                return
            self.config.server_url = result.server_url
            self.config.save()
            where = save_token(result.server_url, result.token)
            stored = "the system keyring" if where == "keyring" else where
            self.say(f"Logged in. Token stored in {stored}.", "ok")
            self.run_worker(self._connect(result.server_url, result.token))

        self.push_screen(LoginScreen(self.config.server_url, can_cancel=can_cancel), done)

    async def _health_tick(self) -> None:
        if not self.api:
            return
        ok = await self.api.health()
        if ok and not self.online:
            self.online = True
            self.header.status = "online"
            self.say("Reconnected to the server.", "ok")
        elif not ok and self.online:
            self.online = False
            self.header.status = "offline"
            self.say("Lost connection to the server. Retrying every 15 s.", "warn")

    async def action_quit(self) -> None:
        self.config.history = self.prompt.history
        if self.player:
            self.config.volume = self.player.state.volume
            self.config.shuffle = self.player.state.shuffle
            self.config.repeat = self.player.state.repeat
        with contextlib.suppress(OSError):
            self.config.save()
        self._save_session()
        if self.player:
            await self.player.shutdown()
        if self.api:
            await self.api.aclose()
        self.exit()

    # --- modes --------------------------------------------------------------------------

    def _on_main_screen(self) -> bool:
        return self.screen is self.screen_stack[0]

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # While capturing a new key in settings, let every key reach that screen.
        from soundable_client.settings import KeyCaptureScreen

        return not isinstance(self.screen, KeyCaptureScreen)

    def set_mode(self, mode: str, initial_text: str = "") -> None:
        if not self._on_main_screen():
            return
        self.mode = mode
        self.header.mode = mode
        self.query_one("#main", ContentSwitcher).current = MODE_VIEWS[mode]
        hints = self.query_one("#hints", Static)
        self.prompt.display = mode == "prompt"
        if mode != "prompt":
            self.query_one(CommandMenu).hide()
        hints.display = mode != "prompt"
        hints.update(Text(mode_hints(mode), style="dim"))
        if mode == "prompt":
            if initial_text:
                self.prompt.value += initial_text
                self.prompt.cursor_position = len(self.prompt.value)
            self.prompt.focus()
        elif mode == "listen":
            self.query_one(ListenView).focus()
            self._refresh_player_views()
        elif mode == "browse":
            browse = self.query_one(BrowseView)
            if not browse.library:
                self.load_library()
            browse.focus_table()

    def action_set_mode(self, mode: str) -> None:
        self.set_mode(mode)

    def action_toggle_browse(self) -> None:
        self.set_mode("prompt" if self.mode == "browse" else "browse")

    # --- player glue ------------------------------------------------------------------

    def player_state(self) -> PlayerState:
        return self.player.state if self.player else PlayerState()

    def _refresh_player_views(self) -> None:
        s = self.player.state if self.player else None
        if s is not None:
            self.query_one(NowPlaying).show(s)
            self.query_one(BrowseView).refresh_queue(s)
        if self.mode == "listen":
            self.query_one(ListenView).show(s)

    def _need_player(self) -> Player | None:
        if not self.player:
            self.say(f"Playback is unavailable: mpv not found. {install_hint()}", "error")
        return self.player

    def player_action(self, what: str) -> None:
        p = self._need_player()
        if not p:
            return
        actions: dict[str, Callable[[], Awaitable[object]]] = {
            "pause": p.toggle_pause,
            "next": p.next,
            "prev": p.prev,
            "vol_up": lambda: p.change_volume(self.config.volume_step),
            "vol_down": lambda: p.change_volume(-self.config.volume_step),
            "back": lambda: p.seek(-self.config.seek_step),
            "fwd": lambda: p.seek(self.config.seek_step),
            "shuffle": self._toggle_shuffle,
            "repeat": self._cycle_repeat,
        }
        self.run_worker(actions[what](), group="player")

    def action_player_action(self, what: str) -> None:
        if not isinstance(self.screen, ModalScreen):
            self.player_action(what)

    # --- settings -----------------------------------------------------------------------

    def action_open_settings(self) -> None:
        from soundable_client.settings import SettingsScreen

        if self._on_main_screen():
            self.push_screen(SettingsScreen())

    async def cmd_settings(self, _: str) -> None:
        self.action_open_settings()

    def update_setting(self, name: str, value: object) -> None:
        if getattr(self.config, name, object()) == value:
            return
        setattr(self.config, name, value)
        with contextlib.suppress(OSError):
            self.config.save()
        if name in ("theme", "accent"):
            self._apply_appearance()
            self.refresh()
        elif name == "normalize" and self.player:
            self.run_worker(self.player.set_normalize(bool(value)))
        elif name == "audio_device" and self.player:
            self.run_worker(self.player.set_audio_device(str(value)))
        elif name == "library_sort":
            self.query_one(BrowseView).library = []  # reload with the new order on next visit

    def set_key(self, action_id: str, key: str | None) -> None:
        if key is None or key == keys.BY_ID[action_id].default:
            self.config.keymap.pop(action_id, None)
        else:
            self.config.keymap[action_id] = key
        with contextlib.suppress(OSError):
            self.config.save()
        self._apply_keymap()

    def reset_keys(self) -> None:
        self.config.keymap.clear()
        with contextlib.suppress(OSError):
            self.config.save()
        self._apply_keymap()

    async def _toggle_shuffle(self) -> None:
        if p := self._need_player():
            await p.set_shuffle(not p.state.shuffle)
            self._announce_modes("shuffle")

    async def _cycle_repeat(self) -> None:
        if p := self._need_player():
            await p.cycle_repeat()
            self._announce_modes("repeat")

    def _announce_modes(self, which: str) -> None:
        s = self.player_state()
        if which == "shuffle":
            if s.shuffle:
                msg = "Shuffle on: the rest of the queue plays in random order"
            else:
                msg = "Shuffle off: back to the original order"
        else:
            msg = {
                "off": "Repeat off",
                "all": "Repeat all: the queue/playlist loops",
                "one": "Repeat one: the current song loops",
            }[s.repeat]
        self.notify(msg, timeout=2)
        self.say(msg)

    @work(group="player")
    async def play_tracks(
        self, tracks: list[Track], start: int = 0, context: str | None = None
    ) -> None:
        p = self._need_player()
        if p and tracks:
            await p.play_list(tracks, start, context=context)
            t = tracks[start]
            self.say(Text.assemble(("Playing ", ""), (t.title, "bold"), " — ", (t.artist, "cyan")))

    @work(group="player")
    async def play_queue_index(self, i: int) -> None:
        p = self._need_player()
        if p:
            await p.play_index(i)

    @work(group="player")
    async def enqueue_tracks(self, tracks: list[Track]) -> None:
        p = self._need_player()
        if p and tracks:
            await p.enqueue(tracks)
            what = f"'{tracks[0].title}'" if len(tracks) == 1 else f"{len(tracks)} tracks"
            self.notify(f"Added {what} to the queue")
            self.say(f"Added {what} to the queue ({len(p.state.queue)} queued).", "ok")

    @work(group="player")
    async def remove_from_queue(self, indexes: list[int]) -> None:
        p = self._need_player()
        if not p:
            return
        for i in sorted(set(indexes), reverse=True):
            if 0 <= i < len(p.state.queue):
                t = await p.remove(i)
                self.say(f"Removed '{t.title}' from the queue.")

    # --- library actions (shared by commands and browse mode) -------------------------

    @work(exclusive=True, group="library")
    async def load_library(self) -> None:
        if not await self._ensure_api():
            return
        assert self.api
        tid = self.tasks.add("Loading library…")
        try:
            tracks = await self.api.all_tracks(sort=self.config.library_sort)
            self.query_one(BrowseView).set_library(tracks)
        except ApiError as e:
            self._api_error(e)
        finally:
            self.tasks.done(tid)

    def edit_track(self, track: Track) -> None:
        async def apply(changes: dict[str, str | None] | None) -> None:
            if not changes or not self.api:
                return
            try:
                updated = await self.api.edit_track(track.id, **changes)
            except ApiError as e:
                self._api_error(e)
                return
            self._replace_track(updated)
            self.say(f"Updated: {updated.title} — {updated.artist}", "ok")

        self.push_screen(EditTrackScreen(track), lambda c: self.run_worker(apply(c)))

    def delete_track(self, track: Track) -> None:
        async def apply(yes: bool | None) -> None:
            if not yes or not self.api:
                return
            try:
                await self.api.delete_track(track.id)
            except ApiError as e:
                self._api_error(e)
                return
            self.last = [t for t in self.last if t.id != track.id]
            browse = self.query_one(BrowseView)
            browse.set_library([t for t in browse.library if t.id != track.id])
            self.say(f"Deleted '{track.title}' — {track.artist}.", "ok")

        q = Text.assemble("Delete ", (track.title, "bold"), " — ", track.artist, "?")
        q.append("\nThe file is removed from the server permanently.", style="dim")
        self.push_screen(ConfirmScreen(q), lambda yes: self.run_worker(apply(yes)))

    def _replace_track(self, updated: Track) -> None:
        def sub(lst: list[Track]) -> list[Track]:
            return [updated if t.id == updated.id else t for t in lst]

        self.last = sub(self.last)
        browse = self.query_one(BrowseView)
        browse.set_library(sub(browse.library))
        if self.player:
            self.player.state.queue = sub(self.player.state.queue)

    # --- playlists (shared by commands and browse mode) -------------------------------

    def _browse(self) -> BrowseView:
        return self.query_one(BrowseView)

    @work(exclusive=True, group="playlists")
    async def load_playlists(self) -> None:
        if not self.api or not self.api.token:
            return
        try:
            self._browse().set_playlists(await self.api.playlists())
        except ApiError as e:
            self._api_error(e)

    async def _after_playlist_change(self, updated: Playlist | None = None) -> None:
        """Refresh browse mode after a playlist mutation."""
        browse = self._browse()
        if updated is not None and browse.open is not None and browse.open.id == updated.id:
            browse.show_playlist(updated)
        if self.api:
            with contextlib.suppress(ApiError):
                browse.set_playlists(await self.api.playlists())

    @work(group="playlists")
    async def open_playlist(self, playlist: Playlist) -> None:
        if not self.api:
            return
        try:
            self._browse().show_playlist(await self.api.playlist(playlist.id), cursor=0)
        except ApiError as e:
            self._api_error(e)

    @work(group="player")
    async def play_playlist(self, playlist: Playlist, shuffle: bool = False) -> None:
        if not self.api or not (p := self._need_player()):
            return
        try:
            full = await self.api.playlist(playlist.id)
        except ApiError as e:
            self._api_error(e)
            return
        tracks = list(full.tracks)
        if not tracks:
            self.say(f"Playlist '{full.name}' is empty.", "warn")
            return
        if shuffle:
            await p.set_shuffle(True)
        start = random.randrange(len(tracks)) if p.state.shuffle else 0
        await p.play_list(tracks, start, context=full.name)
        how = "Shuffling" if p.state.shuffle else "Playing"
        self.say(
            Text.assemble(
                f"{how} playlist ",
                (full.name, f"bold {accent()}"),
                (f"  {len(tracks)} tracks · {fmt_time(full.duration)}", "dim"),
            )
        )

    def add_to_playlist_interactive(self, tracks: list[Track], what: str) -> None:
        """Ask which playlist (or a new one) and add `tracks` to it."""

        async def run() -> None:
            if not self.api:
                return
            try:
                playlists = await self.api.playlists()
            except ApiError as e:
                self._api_error(e)
                return

            async def chosen(choice: str | None) -> None:
                if choice is None:
                    return
                if choice == NEW_PLAYLIST:
                    self.new_playlist_interactive(tracks)
                    return
                target = next(p for p in playlists if p.id == choice)
                await self._add_tracks(target, tracks)

            self.push_screen(
                PlaylistPickerScreen(playlists, what), lambda c: self.run_worker(chosen(c))
            )

        self.run_worker(run(), group="playlists")

    async def _add_tracks(self, playlist: Playlist, tracks: list[Track]) -> None:
        assert self.api
        try:
            updated = await self.api.add_to_playlist(playlist.id, [t.id for t in tracks])
        except ApiError as e:
            self._api_error(e)
            return
        what = f"'{tracks[0].title}'" if len(tracks) == 1 else f"{len(tracks)} tracks"
        n = updated.track_count
        self.say(f"Added {what} to '{updated.name}' ({n} track{'s' * (n != 1)}).", "ok")
        self.notify(f"Added {what} to {updated.name}")
        await self._after_playlist_change(updated)

    def new_playlist_interactive(self, tracks: list[Track]) -> None:
        async def created(name: str | None) -> None:
            if name:
                await self._create_playlist(name, tracks)

        self.push_screen(
            NameScreen("New playlist", placeholder="name"), lambda n: self.run_worker(created(n))
        )

    async def _create_playlist(self, name: str, tracks: list[Track]) -> Playlist | None:
        if not self.api:
            return None
        try:
            p = await self.api.create_playlist(name, [t.id for t in tracks])
        except ApiError as e:
            if e.status in (400, 409):
                self.say(str(e), "warn")
                return None
            self._api_error(e)
            return None
        extra = f" with {p.track_count} tracks" if p.track_count else ""
        self.say(f"Created playlist '{p.name}'{extra}.", "ok")
        await self._after_playlist_change()
        return p

    def rename_playlist_interactive(self, playlist: Playlist) -> None:
        async def renamed(name: str | None) -> None:
            if name and name != playlist.name:
                await self._rename_playlist(playlist, name)

        self.push_screen(
            NameScreen("Rename playlist", initial=playlist.name),
            lambda n: self.run_worker(renamed(n)),
        )

    async def _rename_playlist(self, playlist: Playlist, name: str) -> None:
        assert self.api
        try:
            p = await self.api.rename_playlist(playlist.id, name)
        except ApiError as e:
            if e.status in (400, 409):
                self.say(str(e), "warn")
                return
            self._api_error(e)
            return
        self.say(f"Renamed '{playlist.name}' to '{p.name}'.", "ok")
        await self._after_playlist_change(p)

    def delete_playlist_interactive(self, playlist: Playlist) -> None:
        async def apply(yes: bool | None) -> None:
            if not yes or not self.api:
                return
            try:
                await self.api.delete_playlist(playlist.id)
            except ApiError as e:
                self._api_error(e)
                return
            self.say(f"Deleted playlist '{playlist.name}' (the tracks stay in your library).", "ok")
            await self._after_playlist_change()

        q = Text.assemble("Delete playlist ", (playlist.name, "bold"), "?")
        q.append("\nThe tracks themselves stay in your library.", style="dim")
        self.push_screen(ConfirmScreen(q), lambda yes: self.run_worker(apply(yes)))

    @work(group="playlists")
    async def remove_from_playlist(self, playlist: Playlist, positions: list[int]) -> None:
        if not self.api:
            return
        try:
            p = await self.api.remove_from_playlist(playlist.id, positions)
        except ApiError as e:
            self._api_error(e)
            return
        n = len(positions)
        self.say(f"Removed {n} track{'s' * (n != 1)} from '{p.name}'.")
        await self._after_playlist_change(p)

    @work(group="playlists")
    async def move_in_playlist(self, playlist: Playlist, src: int, dst: int) -> None:
        if not self.api:
            return
        try:
            p = await self.api.move_in_playlist(playlist.id, src, dst)
        except ApiError as e:
            self._api_error(e)
            return
        self._browse().show_playlist(p, cursor=dst)

    # --- /playlist commands -------------------------------------------------------------

    async def _resolve_playlist(self, ref: str) -> Playlist | None:
        """A playlist by number (from the last /playlists listing) or by name
        (exact, then unique prefix/substring, case-insensitive)."""
        assert self.api
        ref = ref.strip()
        if not ref:
            self.say("Which playlist? Give its name or its number from /playlists.", "warn")
            return None
        if ref.isdigit() and self.last_playlists and 1 <= int(ref) <= len(self.last_playlists):
            return self.last_playlists[int(ref) - 1]
        playlists = await self.api.playlists()
        key = fold(ref)
        for matcher in (
            lambda p: fold(p.name) == key,
            lambda p: fold(p.name).startswith(key),
            lambda p: key in fold(p.name),
        ):
            found = [p for p in playlists if matcher(p)]
            if len(found) == 1:
                return found[0]
            if len(found) > 1:
                names = ", ".join(f"'{p.name}'" for p in found[:5])
                self.say(f"'{ref}' matches several playlists: {names}. Be more specific.", "warn")
                return None
        self.say(f"No playlist called '{ref}'. /playlists lists them.", "warn")
        return None

    def _split_ref_numbers(self, rest: str) -> tuple[str, str]:
        """'chill vibes 1 3 5-7' -> ('chill vibes', '1 3 5-7')."""
        tokens = rest.split()
        k = len(tokens)
        while k > 0 and re.fullmatch(r"\d+(-\d+)?", tokens[k - 1]):
            k -= 1
        if k == 0 and tokens:  # only numbers: first one is the playlist number
            k = 1
        return " ".join(tokens[:k]), " ".join(tokens[k:])

    async def _ref_and_numbers(self, rest: str) -> tuple[Playlist | None, str, bool]:
        """Split '<playlist> <numbers…>'. An exact playlist name wins even if it ends with
        digits ('Top 50 3' -> playlist 'Top 50', '3'). Returns (playlist, numbers, had_ref)."""
        assert self.api
        tokens = rest.split()
        by_name = {fold(p.name): p for p in await self.api.playlists()}
        for k in range(len(tokens), 0, -1):
            if p := by_name.get(fold(" ".join(tokens[:k]))):
                return p, " ".join(tokens[k:]), True
        ref, nums = self._split_ref_numbers(rest)
        if not ref:
            return None, nums, False
        return await self._resolve_playlist(ref), nums, True

    async def cmd_playlists(self, _: str) -> None:
        if not await self._ensure_api():
            return
        assert self.api
        playlists = await self.api.playlists()
        self.last_playlists = playlists
        if not playlists:
            self.say(
                "No playlists yet. /playlist new <name>, or /playlist save <name> to "
                "save the current queue."
            )
            return
        table = Table(
            box=box.SIMPLE_HEAD,
            header_style="dim",
            pad_edge=False,
            title="Playlists",
            title_justify="left",
            title_style="bold",
        )
        table.add_column("#", justify="right", style="dim")
        table.add_column("Name", style=f"bold {accent()}")
        table.add_column("Tracks", justify="right")
        table.add_column("Duration", justify="right", style="dim")
        for i, p in enumerate(playlists, 1):
            table.add_row(str(i), p.name, str(p.track_count), fmt_time(p.duration))
        self.show(table)
        self.say(
            Text(
                "/playlist play <n|name> · /playlist show <n|name> · Ctrl+B to browse", style="dim"
            )
        )
        self._browse().set_playlists(playlists)

    async def cmd_playlist(self, args: str) -> None:
        if not await self._ensure_api():
            return
        assert self.api
        sub, _, rest = args.strip().partition(" ")
        sub, rest = sub.lower(), rest.strip()
        if not sub or sub in ("ls", "list"):
            await self.cmd_playlists("")
            return

        if sub in ("new", "create"):
            if not rest:
                self.say("Usage: /playlist new <name>", "warn")
                return
            await self._create_playlist(rest, [])
            return

        if sub == "save":
            queue = self.player_state().queue
            if not rest:
                self.say("Usage: /playlist save <name>  (saves the current queue)", "warn")
                return
            if not queue:
                self.say("The queue is empty: nothing to save.", "warn")
                return
            await self._create_playlist(rest, list(queue))
            return

        if sub in ("play", "shuffle"):
            shuffle = sub == "shuffle"
            if rest.lower().endswith(" shuffle"):
                rest, shuffle = rest[: -len(" shuffle")], True
            if p := await self._resolve_playlist(rest):
                self.play_playlist(p, shuffle=shuffle)
            return

        if sub in ("show", "open"):
            if p := await self._resolve_playlist(rest):
                full = await self.api.playlist(p.id)
                self.last, self.last_kind = list(full.tracks), "playlist"
                self.last_playlist = full
                title = f"{full.name} · {full.track_count} tracks · {fmt_time(full.duration)}"
                self.show(track_table(list(full.tracks), title=title))
                self.say(
                    Text(
                        "/play <n> to play from #n · /playlist rm "
                        f"{full.name} <n> to remove · /playlist move {full.name} <from> <to>",
                        style="dim",
                    )
                )
            return

        if sub == "add":
            p, nums, had_ref = await self._ref_and_numbers(rest)
            source = self._current_list()
            idx = self._numbers(nums, len(source)) if nums else None
            if not had_ref or idx is None:
                self.say(
                    "Usage: /playlist add <name|n> <track numbers…> (numbers from the "
                    "last list shown, e.g. after a search)",
                    "warn",
                )
                return
            if p is not None:
                await self._add_tracks(p, [source[i] for i in idx])
            return

        if sub in ("rm", "remove"):
            p, nums, had_ref = await self._ref_and_numbers(rest)
            if not had_ref:
                self.say("Usage: /playlist rm <name|n> <positions…>", "warn")
                return
            if p is None:
                return
            idx = self._numbers(nums, p.track_count)
            if idx is None:
                self.say(f"Usage: /playlist rm {p.name} <positions 1-{p.track_count}>", "warn")
                return
            self.remove_from_playlist(p, idx)
            return

        if sub == "move":
            p, nums, had_ref = await self._ref_and_numbers(rest)
            parts = nums.split()
            if not had_ref or len(parts) != 2:
                self.say("Usage: /playlist move <name|n> <from> <to>", "warn")
                return
            if p is not None:
                src, dst = int(parts[0]) - 1, int(parts[1]) - 1
                if not (0 <= src < p.track_count and 0 <= dst < p.track_count):
                    self.say(f"Positions must be between 1 and {p.track_count}.", "warn")
                    return
                updated = await self.api.move_in_playlist(p.id, src, dst)
                self.say(f"Moved #{src + 1} to #{dst + 1} in '{updated.name}'.", "ok")
                await self._after_playlist_change(updated)
            return

        if sub == "rename":
            old, sep, new = rest.partition("->")
            if not sep or not old.strip() or not new.strip():
                self.say("Usage: /playlist rename <name|n> -> <new name>", "warn")
                return
            if p := await self._resolve_playlist(old):
                await self._rename_playlist(p, new.strip())
            return

        if sub in ("delete", "del"):
            if p := await self._resolve_playlist(rest):
                self.delete_playlist_interactive(p)
            return

        self.say(
            f"Unknown subcommand '{sub}'. Type /playlist and pick from the menu, or /help.", "warn"
        )

    def _current_list(self) -> list[Track]:
        if self.last_kind == "queue" and self.player:
            return self.player.state.queue
        return self.last

    # --- prompt -----------------------------------------------------------------------

    @on(Input.Submitted, "#prompt")
    def _submitted(self, event: Input.Submitted) -> None:
        line = event.value.strip()
        self.prompt.value = ""
        if not line:
            return
        self.prompt.remember(line)
        self.echo(line)
        self.run_worker(self._handle(line), group="cmd")

    async def _handle(self, line: str) -> None:
        try:
            c = classify(line)
            if c.kind == "command":
                await self._command(line)
            elif c.kind == "import":
                await self.cmd_import(line)
            elif c.kind == "upload":
                await self.upload_paths(list(c.paths))
            elif c.kind == "search":
                await self.cmd_search(line)
        except ApiError as e:
            self._api_error(e)
        except MpvError as e:
            self.say(f"Player error: {e}", "error")

    def _api_error(self, e: ApiError) -> None:
        if isinstance(e, AuthError):
            self.header.status = "logged-out"
            self.say("Session expired or revoked. Please log in again.", "warn")
            self.open_login()
        elif isinstance(e, NetworkError):
            self.online = False
            self.header.status = "offline"
            self.say(f"Network problem: {e}. Retrying in the background.", "error")
        else:
            self.say(f"Server said: {e}", "error")

    async def _ensure_api(self) -> bool:
        if self.api and self.api.token:
            return True
        self.say("Not logged in. Use /login.", "warn")
        return False

    async def _command(self, line: str) -> None:
        name, _, args = line[1:].partition(" ")
        args = args.strip()
        handler = getattr(self, f"cmd_{name.lower()}", None)
        if handler is None:
            matches = match_commands("/" + name.lower())
            hint = f" Did you mean {matches[0].name}?" if matches else ""
            self.say(f"Unknown command /{name}.{hint} Type /help for the list.", "warn")
            return
        await handler(args)

    def _numbers(self, args: str, limit: int) -> list[int] | None:
        """'3', '1 4 6', '2-5' -> 0-based indexes into a list of `limit` items."""
        out: list[int] = []
        for part in args.replace(",", " ").split():
            m = re.fullmatch(r"(\d+)(?:-(\d+))?", part)
            if not m:
                return None
            a, b = int(m[1]), int(m[2] or m[1])
            out += range(a - 1, b)
        if not out or any(i < 0 or i >= limit for i in out):
            return None
        return out

    # --- commands ---------------------------------------------------------------------

    async def cmd_help(self, _: str) -> None:
        self.show(help_text())

    async def cmd_clear(self, _: str) -> None:
        self.log_view.clear()

    async def cmd_quit(self, _: str) -> None:
        await self.action_quit()

    async def cmd_exit(self, args: str) -> None:
        await self.cmd_quit(args)

    async def cmd_library(self, _: str) -> None:
        self.set_mode("browse")

    async def cmd_login(self, _: str) -> None:
        self.open_login()

    async def cmd_logout(self, _: str) -> None:
        if self.api and self.api.token:
            with contextlib.suppress(ApiError):
                await self.api.logout()
        if self.config.server_url:
            delete_token(self.config.server_url)
        if self.player:
            await self.player.clear()
            await self.player.set_token(None)
        if self.api:
            await self.api.aclose()
            self.api = None
        self.header.status = "logged-out"
        self.say("Logged out. The token was revoked on the server and deleted locally.", "ok")

    async def cmd_search(self, args: str) -> None:
        if not args:
            self.say("Usage: /search <words>", "warn")
            return
        if not await self._ensure_api():
            return
        assert self.api
        tracks, total = await self.api.tracks(args, sort="artist", limit=100)
        if not tracks:
            self.say(f"No tracks match '{args}'.")
            return
        more = f" (showing {len(tracks)})" if total > len(tracks) else ""
        self.show_tracks(tracks, f"{total} result{'s' * (total != 1)} for '{args}'{more}")
        self.say(Text("/play <n> to play from #n · /queue add <n> to queue it", style="dim"))

    async def cmd_recent(self, args: str) -> None:
        if not await self._ensure_api():
            return
        assert self.api
        n = int(args) if args.isdigit() else 20
        tracks, total = await self.api.tracks(sort="recent", limit=min(n, 500))
        if not tracks:
            self.say("The library is empty. Drop an audio file here to upload it.")
            return
        self.show_tracks(tracks, f"Recently added ({len(tracks)} of {total})")

    async def cmd_play(self, args: str) -> None:
        p = self._need_player()
        if not p:
            return
        if not args:
            if p.state.current:
                await p.set_pause(False)
            elif self.last:
                self.play_tracks(self.last, 0)
            else:
                await self.cmd_recent("")
                if self.last:
                    self.play_tracks(self.last, 0)
            return
        if re.fullmatch(r"\d+", args):
            n = int(args)
            if self.last_kind == "queue":
                if not 1 <= n <= len(p.state.queue):
                    self.say(f"The queue has {len(p.state.queue)} tracks.", "warn")
                    return
                await p.play_index(n - 1)
                return
            if not 1 <= n <= len(self.last):
                self.say("No such number in the last list. Search first, then /play <n>.", "warn")
                return
            ctx = (
                self.last_playlist.name
                if self.last_kind == "playlist" and self.last_playlist
                else None
            )
            self.play_tracks(self.last, n - 1, context=ctx)
            return
        # free text: search and play the results
        if not await self._ensure_api():
            return
        assert self.api
        tracks, _ = await self.api.tracks(args, sort="artist", limit=200)
        if not tracks:
            self.say(f"Nothing matches '{args}'.")
            return
        self.show_tracks(tracks[:20], f"Playing results for '{args}'")
        self.play_tracks(tracks, 0)

    async def cmd_queue(self, args: str) -> None:
        p = self._need_player()
        if not p:
            return
        sub, _, rest = args.partition(" ")
        if sub == "add":
            if self.last_kind != "tracks" or not self.last:
                self.say("Show some tracks first (search, /recent), then /queue add <n>.", "warn")
                return
            idx = self._numbers(rest, len(self.last))
            if idx is None:
                self.say(f"Usage: /queue add <n…> with numbers 1-{len(self.last)}", "warn")
                return
            self.enqueue_tracks([self.last[i] for i in idx])
            return
        if sub in {"rm", "remove"}:
            idx = self._numbers(rest, len(p.state.queue))
            if idx is None:
                self.say(f"Usage: /queue rm <n…> with numbers 1-{len(p.state.queue)}", "warn")
                return
            self.remove_from_queue(idx)
            return
        if sub == "clear":
            await p.clear()
            self.say("Queue cleared.", "ok")
            return
        if sub:
            self.say("Usage: /queue [add <n…> | rm <n…> | clear]", "warn")
            return
        s = p.state
        if not s.queue:
            self.say("The queue is empty.")
            return
        self.last, self.last_kind = list(s.queue), "queue"
        remaining = sum(t.duration for t in s.queue[max(s.index, 0) :])
        self.show(
            track_table(
                s.queue,
                title=f"Queue · {len(s.queue)} tracks · {fmt_time(remaining)} left",
                current=s.index,
            )
        )

    async def cmd_next(self, _: str) -> None:
        if (p := self._need_player()) and not await p.next():
            self.say("End of the queue.")

    async def cmd_prev(self, _: str) -> None:
        if p := self._need_player():
            await p.prev()

    async def cmd_pause(self, _: str) -> None:
        if p := self._need_player():
            await p.toggle_pause()

    async def cmd_resume(self, _: str) -> None:
        if p := self._need_player():
            await p.set_pause(False)

    async def cmd_stop(self, _: str) -> None:
        if p := self._need_player():
            await p.clear()
            self.say("Stopped and cleared the queue.")

    async def cmd_shuffle(self, args: str) -> None:
        p = self._need_player()
        if not p:
            return
        arg = args.strip().lower()
        if arg not in ("", "on", "off"):
            self.say("Usage: /shuffle [on|off]", "warn")
            return
        target = (not p.state.shuffle) if not arg else arg == "on"
        await p.set_shuffle(target)
        self._announce_modes("shuffle")

    async def cmd_repeat(self, args: str) -> None:
        p = self._need_player()
        if not p:
            return
        aliases = {
            "": None,
            "off": "off",
            "all": "all",
            "playlist": "all",
            "queue": "all",
            "one": "one",
            "song": "one",
            "track": "one",
        }
        arg = args.strip().lower()
        if arg not in aliases:
            self.say("Usage: /repeat [off|all|one]", "warn")
            return
        mode = aliases[arg]
        if mode is None:
            await p.cycle_repeat()
        else:
            await p.set_repeat(mode)
        self._announce_modes("repeat")

    async def cmd_seek(self, args: str) -> None:
        p = self._need_player()
        if not p:
            return
        m = re.fullmatch(r"([+-])?(?:(\d+):)?(\d+)", args.strip())
        if not m:
            self.say("Usage: /seek +30 · /seek -10 · /seek 1:20", "warn")
            return
        secs = int(m[2] or 0) * 60 + int(m[3])
        if m[1]:
            await p.seek(secs if m[1] == "+" else -secs)
        else:
            await p.seek_to(secs)

    async def cmd_vol(self, args: str) -> None:
        p = self._need_player()
        if not p:
            return
        m = re.fullmatch(r"([+-])?(\d{1,3})", args.strip())
        if not m:
            self.say(f"Volume is {p.state.volume}%. Usage: /vol 60 · /vol +5 · /vol -5")
            return
        v = int(m[2])
        new = await (p.change_volume(v if m[1] == "+" else -v) if m[1] else p.set_volume(v))
        self.say(f"Volume {new}%")

    async def cmd_volume(self, args: str) -> None:
        await self.cmd_vol(args)

    async def cmd_add(self, args: str) -> None:
        if not args:
            self.say("Usage: /add <file|folder path…> or /add <YouTube/SoundCloud link>", "warn")
            return
        c = classify(args)
        if c.kind == "import":
            await self.cmd_import(args)
        elif c.kind == "upload":
            await self.upload_paths(list(c.paths))
        else:
            missing = [p for p in split_paths(args) if not Path(p).exists()]
            self.say(f"Not found: {', '.join(missing) or args}", "error")

    async def cmd_import(self, url: str) -> None:
        if not await self._ensure_api():
            return
        assert self.api
        try:
            job, created = await self.api.create_import(url)
        except ApiError as e:
            if e.status == 409 and isinstance(e.data, dict) and e.data.get("existing_track_id"):
                track = await self.api.track(e.data["existing_track_id"])
                self.last, self.last_kind = [track], "tracks"
                self.say(
                    Text.assemble(
                        "Already in your library: ",
                        (track.title, "bold"),
                        " — ",
                        (track.artist, "cyan"),
                        ("  · /play 1 to play it", "dim"),
                    ),
                    "warn",
                )
                return
            if e.status == 400:
                self.say(str(e), "warn")
                return
            raise
        if not created:
            self.say("That link is already being imported.")
        self.track_job(job)

    def track_job(self, job: Job) -> None:
        if job.id in self._tracked_jobs:
            return
        self._tracked_jobs.add(job.id)
        self.run_worker(self._follow_job(job), group="jobs")

    async def _follow_job(self, job: Job) -> None:
        """Spinner line that follows a server-side import until it finishes."""
        tid = self.tasks.add(f"Queued: {job.label}")
        failures = 0
        try:
            while True:
                if job.status == "running":
                    self.tasks.update_task(
                        tid, label=f"Importing {job.label}", progress=job.progress / 100
                    )
                elif job.status == "queued":
                    state = f"Retrying soon ({job.error})" if job.error else "Queued"
                    self.tasks.update_task(tid, label=f"{state}: {job.label}")
                else:
                    break
                await asyncio.sleep(1.0)
                if not self.api:
                    return
                try:
                    job = await self.api.job(job.id)
                    failures = 0
                except NetworkError:
                    failures += 1  # keep following; the header shows we're offline
                    await asyncio.sleep(min(30, 2 * failures))
                except ApiError as e:
                    self._api_error(e)
                    return
        finally:
            self.tasks.done(tid)
            self._tracked_jobs.discard(job.id)

        if job.status == "done" and job.track:
            t = job.track
            self.last, self.last_kind = [t], "tracks"
            self.say(
                Text.assemble(
                    ("Imported ", ""),
                    (t.title, "bold"),
                    " — ",
                    (t.artist, "cyan"),
                    (f"  {fmt_time(t.duration)} · {t.codec or ''} · {t.source}", "dim"),
                ),
                "ok",
            )
            self.say(Text("/play 1 to play · /edit 1 to fix the title/artist", style="dim"))
            browse = self.query_one(BrowseView)
            if browse.library:
                browse.set_library([*browse.library, t])
        elif job.track:  # failed as a duplicate: the server tells us which track
            self.last, self.last_kind = [job.track], "tracks"
            self.say(f"{job.error}  · /play 1 to play it", "warn")
        else:
            self.say(f"Import failed: {job.error or 'unknown error'}  ({job.url})", "error")

    async def cmd_jobs(self, args: str) -> None:
        if not await self._ensure_api():
            return
        assert self.api
        jobs = await self.api.jobs(limit=15)
        sub, _, rest = args.partition(" ")
        if sub == "retry":
            failed = [j for j in jobs if j.status == "failed"]
            idx = self._numbers(rest, len(jobs))
            if idx is None or len(idx) != 1 or jobs[idx[0]].status != "failed":
                hint = " (no failed jobs)" if not failed else ""
                self.say(f"Usage: /jobs retry <n> with the # of a failed job{hint}", "warn")
                return
            job = await self.api.retry_job(jobs[idx[0]].id)
            self.say(f"Retrying {job.label}")
            self.track_job(job)
            return
        if not jobs:
            self.say("No imports yet. Paste a YouTube or SoundCloud link to import one.")
            return
        table = Table(
            box=box.SIMPLE_HEAD,
            header_style="dim",
            pad_edge=False,
            title="Recent imports",
            title_justify="left",
            title_style="bold",
        )
        table.add_column("#", justify="right", style="dim")
        table.add_column("Status")
        table.add_column("Track", overflow="ellipsis", max_width=50)
        table.add_column("Details", style="dim", overflow="fold", max_width=60)
        styles = {"done": "green", "failed": "red", "running": accent(), "queued": "yellow"}
        for i, j in enumerate(jobs, 1):
            status = j.status + (f" {j.progress}%" if j.status == "running" else "")
            detail = j.error or (j.track.source if j.track else j.url or "")
            table.add_row(str(i), Text(status, style=styles.get(j.status, "")), j.label, detail)
        self.show(table)
        if any(j.status == "failed" for j in jobs):
            self.say(Text("/jobs retry <n> to retry a failed import", style="dim"))

    def _pick(self, args: str, verb: str) -> Track | None:
        in_queue = self.last_kind == "queue" and self.player is not None
        source = self.player.state.queue if in_queue and self.player else self.last
        idx = self._numbers(args, len(source)) if args else None
        if idx is None or len(idx) != 1:
            self.say(f"Usage: /{verb} <n>, where n is a number from the last list.", "warn")
            return None
        return source[idx[0]]

    async def cmd_edit(self, args: str) -> None:
        if (t := self._pick(args, "edit")) and await self._ensure_api():
            self.edit_track(t)

    async def cmd_delete(self, args: str) -> None:
        if (t := self._pick(args, "delete")) and await self._ensure_api():
            self.delete_track(t)

    # --- uploads ----------------------------------------------------------------------

    async def upload_paths(self, paths: list[Path]) -> None:
        if not await self._ensure_api():
            return
        assert self.api
        files = expand_audio_paths(paths)
        skipped = [f for f in files if f.suffix.lower() not in AUDIO_EXTENSIONS]
        files = [f for f in files if f.suffix.lower() in AUDIO_EXTENSIONS]
        for f in skipped:
            self.say(f"Skipped {f.name}: not an audio file extension.", "warn")
        if not files:
            self.say("No audio files to upload.", "warn")
            return
        added: list[Track] = []
        for n, f in enumerate(files, 1):
            prefix = f"[{n}/{len(files)}] " if len(files) > 1 else ""
            size = f.stat().st_size
            tid = self.tasks.add(f"{prefix}Uploading {f.name} ({fmt_size(size)})")

            def progress(sent: int, total: int, tid: int = tid) -> None:
                self.tasks.update_task(tid, progress=sent / total if total else None)

            try:
                track = await self.api.upload(f, progress)
            except ApiError as e:
                if e.status == 409:
                    self.say(f"{f.name}: already in your library.", "warn")
                elif isinstance(e, AuthError | NetworkError):
                    self.tasks.done(tid)
                    self._api_error(e)
                    return
                else:
                    self.say(f"{f.name}: {e}", "error")
                continue
            finally:
                self.tasks.done(tid)
            added.append(track)
            self.say(
                Text.assemble(
                    ("Added ", ""),
                    (track.title, "bold"),
                    " — ",
                    (track.artist, "cyan"),
                    (f"  {fmt_time(track.duration)} · {track.codec or ''}", "dim"),
                ),
                "ok",
            )
            await asyncio.sleep(0)
        if len(added) > 1:
            self.show_tracks(added, f"Uploaded {len(added)} tracks")
        elif added:
            self.last, self.last_kind = added, "tracks"
        if added:
            self.say(Text("/play 1 to play · /edit 1 to fix the title/artist", style="dim"))
            browse = self.query_one(BrowseView)
            if browse.library:
                browse.set_library(browse.library + added)
