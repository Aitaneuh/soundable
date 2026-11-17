"""The soundable Textual app: Claude-Code-style log + prompt, plus listening/browse modes."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from rich.console import RenderableType
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import ContentSwitcher, Input, RichLog, Static

from soundable_client import __version__
from soundable_client.api import Api, ApiError, AuthError, NetworkError, Track
from soundable_client.config import Config, delete_token, load_token, save_token
from soundable_client.formatting import ACCENT, fmt_size, fmt_time, track_table
from soundable_client.inputs import AUDIO_EXTENSIONS, classify, expand_audio_paths, split_paths
from soundable_client.mpv import Mpv, MpvError, find_mpv, install_hint
from soundable_client.player import Player, PlayerState
from soundable_client.screens import ConfirmScreen, EditTrackScreen, LoginResult, LoginScreen
from soundable_client.views import BrowseView, ListenView, help_text
from soundable_client.widgets import NowPlaying, PromptInput, StatusHeader, TaskList

COMMANDS = [
    "/search ",
    "/recent",
    "/play ",
    "/queue",
    "/queue add ",
    "/queue rm ",
    "/queue clear",
    "/next",
    "/prev",
    "/pause",
    "/stop",
    "/seek ",
    "/vol ",
    "/add ",
    "/edit ",
    "/delete ",
    "/library",
    "/login",
    "/logout",
    "/clear",
    "/help",
    "/quit",
]

MODE_VIEWS = {"prompt": "log", "listen": "listen", "browse": "browse"}
MODE_HINTS = {
    "listen": "←/→ prev/next · ↑/↓ volume · space pause · ,/. seek ±10s · "
    "Esc prompt · Ctrl+B browse",
    "browse": "↑/↓ move · Enter play · a queue · e edit · d delete · Tab library/queue · "
    "/ filter · space pause · Esc prompt",
}


class SoundableApp(App[None]):
    CSS_PATH = "app.tcss"
    TITLE = "soundable"
    ENABLE_COMMAND_PALETTE = False
    BINDINGS = [
        Binding("ctrl+b", "toggle_browse", "browse", priority=True),
        Binding("ctrl+q", "quit", "quit", priority=True),
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

    # --- layout -----------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        yield StatusHeader(id="header")
        with ContentSwitcher(initial="log", id="main"):
            yield RichLog(id="log", wrap=True, markup=False, auto_scroll=True, min_width=40)
            yield ListenView(id="listen")
            yield BrowseView(id="browse")
        yield TaskList(id="tasks")
        yield NowPlaying(id="np")
        yield PromptInput(COMMANDS, self.config.history, id="prompt")
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

    def say(self, message: str | Text, kind: str = "info") -> None:
        color = {"info": "white", "ok": "green", "warn": "yellow", "error": "red"}[kind]
        t = Text()
        t.append("⏺ ", style=color if kind != "info" else ACCENT)
        t.append_text(message if isinstance(message, Text) else Text(message))
        self.log_view.write(t)

    def show(self, renderable: RenderableType) -> None:
        self.log_view.write(renderable)

    def echo(self, line: str) -> None:
        t = Text()
        t.append("› ", style="bold dim")
        t.append(line, style="bold")
        self.log_view.write(Text(""))
        self.log_view.write(t)

    def show_tracks(self, tracks: list[Track], title: str | None = None) -> None:
        self.last, self.last_kind = tracks, "tracks"
        self.show(track_table(tracks, title=title))

    # --- lifecycle --------------------------------------------------------------------

    def on_mount(self) -> None:
        self.header.server = self.config.server_url
        self.prompt.focus()
        welcome = Text()
        welcome.append("✻ ", style=f"bold {ACCENT}")
        welcome.append(f"soundable {__version__}", style="bold")
        welcome.append("  ·  /help for commands  ·  Esc listening mode  ·  Ctrl+B browse")
        self.log_view.write(welcome)
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
        try:
            await self.player.start()
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
        with contextlib.suppress(OSError):
            self.config.save()
        if self.player:
            await self.player.shutdown()
        if self.api:
            await self.api.aclose()
        self.exit()

    # --- modes --------------------------------------------------------------------------

    def set_mode(self, mode: str, initial_text: str = "") -> None:
        if isinstance(self.screen, ModalScreen):
            return
        self.mode = mode
        self.header.mode = mode
        self.query_one("#main", ContentSwitcher).current = MODE_VIEWS[mode]
        hints = self.query_one("#hints", Static)
        self.prompt.display = mode == "prompt"
        hints.display = mode != "prompt"
        hints.update(Text(MODE_HINTS.get(mode, ""), style="dim"))
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
            "vol_up": lambda: p.change_volume(5),
            "vol_down": lambda: p.change_volume(-5),
            "back": lambda: p.seek(-10),
            "fwd": lambda: p.seek(10),
        }
        self.run_worker(actions[what](), group="player")

    @work(group="player")
    async def play_tracks(self, tracks: list[Track], start: int = 0) -> None:
        p = self._need_player()
        if p and tracks:
            await p.play_list(tracks, start)
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
            tracks = await self.api.all_tracks(sort="artist")
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
            matches = [c.strip() for c in COMMANDS if c.startswith("/" + name.lower())]
            hint = f" Did you mean {matches[0]}?" if matches else ""
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
            self.play_tracks(self.last, n - 1)
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
        self.say(
            "Link imports (YouTube/SoundCloud) arrive in the next milestone. "
            "For now you can upload audio files.",
            "warn",
        )

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
