"""The listening and browse views (the prompt view is a plain RichLog)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.align import Align
from rich.console import Group
from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widget import Widget
from textual.widgets import ContentSwitcher, DataTable, Input, Static

from soundable_client import keys
from soundable_client.api import Playlist, Track
from soundable_client.commands import COMMAND_SPECS
from soundable_client.formatting import accent, fmt_time, fold, progress_bar, track_table
from soundable_client.player import PlayerState

if TYPE_CHECKING:
    from soundable_client.app import SoundableApp


class ListenView(Widget, can_focus=True):
    """Full-screen now playing. ←/→ track, ↑/↓ volume, space pause."""

    BINDINGS = [
        keys.binding("listen.prev", "player('prev')"),
        keys.binding("listen.next", "player('next')"),
        keys.binding("listen.vol_up", "player('vol_up')"),
        keys.binding("listen.vol_down", "player('vol_down')"),
        keys.binding("listen.pause", "player('pause')"),
        keys.binding("listen.back", "player('back')"),
        keys.binding("listen.fwd", "player('fwd')"),
        keys.binding("listen.shuffle", "player('shuffle')"),
        keys.binding("listen.repeat", "player('repeat')"),
        Binding("escape", "app.set_mode('prompt')", "prompt"),
    ]

    DEFAULT_CSS = """
    ListenView { height: 1fr; padding: 1 2; }
    ListenView #np-big { height: auto; margin-bottom: 1; text-align: center; }
    ListenView #upnext { height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        yield Static(id="np-big")
        yield Static(id="upnext")

    @property
    def sapp(self) -> SoundableApp:
        return self.app  # type: ignore[return-value]

    def action_player(self, what: str) -> None:
        self.sapp.player_action(what)

    def on_key(self, event: events.Key) -> None:
        # Start typing anywhere -> back to the prompt with that character.
        if event.key in keys.scope_keys("listen"):
            return  # handled by a (possibly remapped) binding
        if event.is_printable and event.character:
            event.stop()
            self.sapp.set_mode("prompt", initial_text=event.character)

    def show(self, s: PlayerState | None) -> None:
        big = self.query_one("#np-big", Static)
        upnext = self.query_one("#upnext", Static)
        width = max(self.size.width - 4, 30)
        if s is None or s.current is None:
            big.update(Align.center(Text("\n\nnothing playing\n", style="dim")))
            upnext.update(
                Text("Esc to go back to the prompt, then search and /play something.", style="dim")
            )
            return
        t = s.current
        lines = Text(justify="center")
        lines.append("\n")
        lines.append(("⏸  " if s.paused else "▶  "), style=f"bold {accent()}")
        lines.append(t.title + "\n", style="bold")
        lines.append(t.artist + "\n", style="cyan")
        if t.album:
            lines.append(t.album + "\n", style="dim")
        lines.append("\n")
        dur = s.duration or t.duration
        bar_w = min(width - 16, 60)
        lines.append(fmt_time(s.position) + "  ", style="dim")
        lines.append_text(progress_bar(s.position / dur if dur else 0, bar_w))
        lines.append("  " + fmt_time(dur) + "\n\n", style="dim")
        lines.append("vol ", style="dim")
        lines.append_text(progress_bar(s.volume / 100, 20, color="cyan"))
        lines.append(f" {s.volume}%", style="dim")
        lines.append("\n\n")
        lines.append("⇄ shuffle", style=f"bold {accent()}" if s.shuffle else "grey35")
        lines.append("    ")
        rep = {"off": "↻ repeat off", "all": "↻ repeat all", "one": "↻ repeat one"}[s.repeat]
        lines.append(rep, style=f"bold {accent()}" if s.repeat != "off" else "grey35")
        big.update(lines)

        rest = s.queue[s.index + 1 :]
        if rest:
            upnext.update(track_table(rest[:50], title="Up next", start=s.index + 2))
        else:
            upnext.update(Text("End of queue.", style="dim"))


def pane_hints(pane: str) -> str:
    k = keys.show
    if pane == "lib":
        return (
            f"Enter play · {k('browse.enqueue')} queue · {k('browse.add_to_playlist')} add to "
            f"playlist · {k('browse.edit')} edit · {k('browse.delete')} delete · "
            f"{k('browse.filter')} filter · {k('browse.refresh')} refresh"
        )
    if pane == "pls":
        return (
            f"Enter play · → open · {k('browse.new_playlist')} new · {k('browse.edit')} rename "
            f"· {k('browse.delete')} delete"
        )
    if pane == "plt":
        return (
            f"Enter play from here · ← back · {k('browse.add_to_playlist')} add to another "
            f"playlist · {k('browse.delete')} remove · {k('browse.move_up')}/"
            f"{k('browse.move_down')} move · {k('browse.edit')} edit track"
        )
    return (
        f"Enter jump to · {k('browse.add_to_playlist')} add to playlist · "
        f"{k('browse.delete')} remove · {k('browse.edit')} edit track"
    )


class BrowseView(Vertical):
    """Navigate the library, playlists and the queue with the keyboard."""

    BINDINGS = [
        Binding("tab", "switch_pane(1)", "next tab", priority=True),
        Binding("shift+tab", "switch_pane(-1)", "previous tab", priority=True),
        Binding("right,o", "open_playlist", "open", priority=True),
        Binding("left,backspace", "close_playlist", "back", priority=True),
        keys.binding("browse.enqueue", "enqueue"),
        keys.binding("browse.add_to_playlist", "add_to_playlist"),
        keys.binding("browse.new_playlist", "new_playlist"),
        keys.binding("browse.edit", "edit"),
        keys.binding("browse.delete", "delete"),
        keys.binding("browse.move_up", "move(-1)"),
        keys.binding("browse.move_down", "move(1)"),
        keys.binding("browse.refresh", "refresh"),
        keys.binding("browse.filter", "filter"),
        keys.binding("browse.pause", "pause"),
        Binding("escape", "back", "back"),
    ]

    DEFAULT_CSS = """
    BrowseView { height: 1fr; }
    BrowseView #browse-tabs { height: 2; padding: 0 1; }
    BrowseView #filter { display: none; margin: 0 1; }
    BrowseView #filter.visible { display: block; }
    BrowseView DataTable { height: 1fr; }
    """

    def __init__(self, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self.library: list[Track] = []
        self.filtered: list[Track] = []
        self.playlists: list[Playlist] = []
        self.open: Playlist | None = None  # playlist shown in the "plt" pane
        self.pane = "lib"
        self.playlists_loaded = False
        self._queue_sig: tuple[int, int] | None = None

    @property
    def sapp(self) -> SoundableApp:
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        yield Static(id="browse-tabs")
        yield Input(placeholder="filter (Enter to go to the list, Esc to clear)", id="filter")
        with ContentSwitcher(initial="lib"):
            yield DataTable(id="lib", cursor_type="row", zebra_stripes=True)
            yield DataTable(id="pls", cursor_type="row", zebra_stripes=True)
            yield DataTable(id="plt", cursor_type="row", zebra_stripes=True)
            yield DataTable(id="queue", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        for tid in ("#lib", "#plt", "#queue"):
            table = self.query_one(tid, DataTable)
            table.add_column("#", key="n", width=4)
            table.add_column("Title", key="title", width=40)
            table.add_column("Artist", key="artist", width=26)
            table.add_column("Album", key="album", width=22)
            table.add_column("Time", key="time", width=7)
        pls = self.query_one("#pls", DataTable)
        pls.add_column("#", width=4)
        pls.add_column("Playlist", width=40)
        pls.add_column("Tracks", width=8)
        pls.add_column("Duration", width=10)
        self._render_tabs()

    # --- tabs -----------------------------------------------------------------------------

    def _render_tabs(self) -> None:
        q = len(self.sapp.player_state().queue)
        pl_label = f"Playlists ({len(self.playlists)})"
        if self.open is not None:
            pl_label = f"Playlists ▸ {self.open.name}"
        tabs = (
            ("lib", f"Library ({len(self.filtered)})"),
            ("pl", pl_label),
            ("queue", f"Queue ({q})"),
        )
        active = "pl" if self.pane in ("pls", "plt") else self.pane
        t = Text(" ")
        for key, label in tabs:
            style = f"bold reverse {accent()}" if key == active else "dim"
            t.append(f" {label} ", style=style)
            t.append("  ")
        t.append("\n ")
        t.append(pane_hints(self.pane), style="dim")
        self.query_one("#browse-tabs", Static).update(t)

    def _show(self, pane: str) -> None:
        self.pane = pane
        self.query_one(ContentSwitcher).current = pane
        if pane == "queue":
            self.refresh_queue(self.sapp.player_state(), force=True)
        if pane == "pls" and not self.playlists_loaded:
            self.sapp.load_playlists()
        self._render_tabs()
        self.focus_table()

    def focus_table(self) -> None:
        self.query_one(f"#{self.pane}", DataTable).focus()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # Priority bindings must not steal keys from the filter box.
        if action in ("switch_pane", "open_playlist", "close_playlist"):
            if self.query_one("#filter", Input).has_focus:
                return action == "switch_pane"
            if action == "open_playlist":
                return self.pane == "pls"
            if action == "close_playlist":
                return self.pane == "plt"
        return True

    def action_switch_pane(self, step: int) -> None:
        if self.query_one("#filter", Input).has_focus:
            self.focus_table()
            return
        order = ["lib", "pl", "queue"]
        cur = "pl" if self.pane in ("pls", "plt") else self.pane
        nxt = order[(order.index(cur) + step) % len(order)]
        if nxt == "pl":
            nxt = "plt" if self.open is not None else "pls"
        self._show(nxt)

    # --- data -------------------------------------------------------------------------

    def set_library(self, tracks: list[Track]) -> None:
        self.library = tracks
        self.apply_filter(self.query_one("#filter", Input).value)

    def apply_filter(self, text: str) -> None:
        words = fold(text).split()
        if words:
            self.filtered = [
                t
                for t in self.library
                if all(w in fold(f"{t.title} {t.artist} {t.album or ''}") for w in words)
            ]
        else:
            self.filtered = list(self.library)
        _fill_tracks(self.query_one("#lib", DataTable), self.filtered)
        self._render_tabs()

    def set_playlists(self, playlists: list[Playlist]) -> None:
        self.playlists = playlists
        self.playlists_loaded = True
        table = self.query_one("#pls", DataTable)
        row = table.cursor_row
        table.clear()
        for i, p in enumerate(playlists, 1):
            table.add_row(str(i), p.name, str(p.track_count), fmt_time(p.duration))
        if playlists:
            table.move_cursor(row=min(row, len(playlists) - 1))
        if self.open is not None and all(p.id != self.open.id for p in playlists):
            self.open = None
            if self.pane == "plt":
                self._show("pls")
        self._render_tabs()

    def show_playlist(self, playlist: Playlist, cursor: int | None = None) -> None:
        """Open (or refresh) a playlist's track list."""
        self.open = playlist
        table = self.query_one("#plt", DataTable)
        row = table.cursor_row if cursor is None else cursor
        _fill_tracks(table, list(playlist.tracks))
        if playlist.tracks:
            table.move_cursor(row=max(0, min(row, len(playlist.tracks) - 1)))
        if self.pane != "plt":
            self._show("plt")
        self._render_tabs()

    def refresh_queue(self, s: PlayerState, force: bool = False) -> None:
        sig = (len(s.queue), s.index)
        if sig == self._queue_sig and not force:
            return
        self._queue_sig = sig
        table = self.query_one("#queue", DataTable)
        row = table.cursor_row
        _fill_tracks(table, s.queue, current=s.index)
        if s.queue:
            table.move_cursor(row=min(row, len(s.queue) - 1))
        self._render_tabs()

    # --- actions ----------------------------------------------------------------------

    def _cursor(self) -> int | None:
        table = self.query_one(f"#{self.pane}", DataTable)
        return table.cursor_row if table.row_count else None

    def _track_list(self) -> list[Track]:
        if self.pane == "lib":
            return self.filtered
        if self.pane == "plt" and self.open is not None:
            return list(self.open.tracks)
        if self.pane == "queue":
            return self.sapp.player_state().queue
        return []

    def _selected_track(self) -> Track | None:
        i = self._cursor()
        tracks = self._track_list()
        return tracks[i] if i is not None and i < len(tracks) else None

    def _selected_playlist(self) -> Playlist | None:
        i = self._cursor()
        if self.pane == "pls" and i is not None and i < len(self.playlists):
            return self.playlists[i]
        return None

    @on(DataTable.RowSelected)
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        i = event.cursor_row
        if self.pane == "lib":
            self.sapp.play_tracks(self.filtered, i)
        elif self.pane == "queue":
            self.sapp.play_queue_index(i)
        elif self.pane == "pls" and (p := self._selected_playlist()):
            self.sapp.play_playlist(p)
        elif self.pane == "plt" and self.open is not None:
            self.sapp.play_tracks(list(self.open.tracks), i, context=self.open.name)

    def action_open_playlist(self) -> None:
        if p := self._selected_playlist():
            self.sapp.open_playlist(p)

    def action_close_playlist(self) -> None:
        self.open = None
        self._show("pls")

    def action_enqueue(self) -> None:
        if self.pane in ("lib", "plt") and (t := self._selected_track()):
            self.sapp.enqueue_tracks([t])

    def action_add_to_playlist(self) -> None:
        if t := self._selected_track():
            self.sapp.add_to_playlist_interactive([t], t.title)

    def action_new_playlist(self) -> None:
        if self.pane in ("pls", "plt"):
            self.sapp.new_playlist_interactive([])

    def action_edit(self) -> None:
        if self.pane == "pls":
            if p := self._selected_playlist():
                self.sapp.rename_playlist_interactive(p)
        elif t := self._selected_track():
            self.sapp.edit_track(t)

    def action_delete(self) -> None:
        i = self._cursor()
        if i is None:
            return
        if self.pane == "lib":
            self.sapp.delete_track(self.filtered[i])
        elif self.pane == "queue":
            self.sapp.remove_from_queue([i])
        elif self.pane == "pls" and (p := self._selected_playlist()):
            self.sapp.delete_playlist_interactive(p)
        elif self.pane == "plt" and self.open is not None:
            self.sapp.remove_from_playlist(self.open, [i])

    def action_move(self, step: int) -> None:
        i = self._cursor()
        if self.pane == "plt" and self.open is not None and i is not None:
            j = i + step
            if 0 <= j < len(self.open.tracks):
                self.sapp.move_in_playlist(self.open, i, j)

    def action_refresh(self) -> None:
        self.sapp.load_library()
        self.sapp.load_playlists()

    def action_pause(self) -> None:
        self.sapp.player_action("pause")

    def action_filter(self) -> None:
        if self.pane != "lib":
            self._show("lib")
        f = self.query_one("#filter", Input)
        f.add_class("visible")
        f.focus()

    def action_back(self) -> None:
        f = self.query_one("#filter", Input)
        if f.has_focus:
            f.value = ""
            f.remove_class("visible")
            self.focus_table()
        elif self.pane == "plt":
            self.action_close_playlist()
        else:
            self.sapp.set_mode("prompt")

    @on(Input.Changed, "#filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        self.apply_filter(event.value)

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.focus_table()


def _fill_tracks(table: DataTable, tracks: list[Track], current: int | None = None) -> None:
    table.clear()
    for i, t in enumerate(tracks):
        is_cur = current is not None and i == current
        style = f"bold {accent()}" if is_cur else ""
        table.add_row(
            Text("▶" if is_cur else str(i + 1), style=style),
            Text(t.title, style=style),
            t.artist,
            t.album or "",
            fmt_time(t.duration),
        )


def help_text() -> Group:
    out: list[Text] = []

    def section(title: str) -> None:
        out.append(Text(title, style="bold"))

    def row(left: str, right: str) -> None:
        t = Text("  ")
        t.append(f"{left:<30}", style="cyan")
        t.append(right, style="dim")
        out.append(t)

    section("Just type:")
    row("<YouTube/SoundCloud link>", "import it on the server")
    row("<file or folder path(s)>", "upload (drag & drop works)")
    row("<anything else>", "search the library")
    out.append(Text())
    section("Commands (type / to see them as you type, Tab/Enter to complete):")
    for c in COMMAND_SPECS:
        row(f"{c.name} {c.args}".strip(), c.help)
    out.append(Text())
    section("Modes:")
    row("Esc", "prompt <-> listening (←/→ track, ↑/↓ volume, space pause, ,/. seek)")
    row("Ctrl+B", "browse (library & queue, Enter play, / filter, space pause)")
    row("Ctrl+S / Ctrl+R", "shuffle on/off · repeat off -> all -> one (any mode)")
    row("Media keys", "play/pause/next/prev work system-wide (via mpv)")
    row("F2 or /settings", "theme, accent, key remapping, playback options")
    row("Ctrl+Q", "quit")
    return Group(*out)
