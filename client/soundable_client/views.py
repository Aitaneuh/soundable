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

from soundable_client.api import Track
from soundable_client.commands import COMMAND_SPECS
from soundable_client.formatting import ACCENT, fmt_time, fold, progress_bar, track_table
from soundable_client.player import PlayerState

if TYPE_CHECKING:
    from soundable_client.app import SoundableApp


class ListenView(Widget, can_focus=True):
    """Full-screen now playing. ←/→ track, ↑/↓ volume, space pause."""

    BINDINGS = [
        Binding("left", "player('prev')", "prev"),
        Binding("right", "player('next')", "next"),
        Binding("up", "player('vol_up')", "vol +5"),
        Binding("down", "player('vol_down')", "vol -5"),
        Binding("space", "player('pause')", "play/pause"),
        Binding("shift+left,comma", "player('back')", "-10s"),
        Binding("shift+right,full_stop", "player('fwd')", "+10s"),
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
        if event.is_printable and event.character and event.character not in " ,.":
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
        lines.append(("⏸  " if s.paused else "▶  "), style=f"bold {ACCENT}")
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
        big.update(lines)

        rest = s.queue[s.index + 1 :]
        if rest:
            upnext.update(track_table(rest[:50], title="Up next", start=s.index + 2))
        else:
            upnext.update(Text("End of queue.", style="dim"))


class BrowseView(Vertical):
    """Navigate the library and the queue with the keyboard."""

    BINDINGS = [
        Binding("tab", "switch_pane", "library/queue", priority=True),
        Binding("a", "enqueue", "add to queue"),
        Binding("e", "edit", "edit"),
        Binding("d,delete", "delete", "delete/remove"),
        Binding("r", "refresh", "refresh"),
        Binding("slash", "filter", "filter"),
        Binding("space", "pause", "play/pause"),
        Binding("escape", "back", "prompt"),
    ]

    DEFAULT_CSS = """
    BrowseView { height: 1fr; }
    BrowseView #browse-tabs { height: 1; padding: 0 1; }
    BrowseView #filter { display: none; margin: 0 1; }
    BrowseView #filter.visible { display: block; }
    BrowseView DataTable { height: 1fr; }
    """

    def __init__(self, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self.library: list[Track] = []
        self.filtered: list[Track] = []
        self.pane = "lib"
        self._queue_sig: tuple[int, int] | None = None

    @property
    def sapp(self) -> SoundableApp:
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        yield Static(id="browse-tabs")
        yield Input(placeholder="filter (Enter to go to the list, Esc to clear)", id="filter")
        with ContentSwitcher(initial="lib"):
            yield DataTable(id="lib", cursor_type="row", zebra_stripes=True)
            yield DataTable(id="queue", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        for tid in ("#lib", "#queue"):
            table = self.query_one(tid, DataTable)
            table.add_column("#", key="n", width=4)
            table.add_column("Title", key="title", width=40)
            table.add_column("Artist", key="artist", width=26)
            table.add_column("Album", key="album", width=22)
            table.add_column("Time", key="time", width=6)
        self._render_tabs()

    def _render_tabs(self) -> None:
        t = Text(" ")
        for key, label in (("lib", f"Library ({len(self.filtered)})"), ("queue", "Queue")):
            style = f"bold reverse {ACCENT}" if key == self.pane else "dim"
            t.append(f" {label} ", style=style)
            t.append("  ")
        t.append("Tab switch · Enter play · a queue · e edit · d delete · / filter", style="dim")
        self.query_one("#browse-tabs", Static).update(t)

    def focus_table(self) -> None:
        self.query_one(f"#{self.pane}", DataTable).focus()

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
        table = self.query_one("#lib", DataTable)
        table.clear()
        for i, t in enumerate(self.filtered, 1):
            table.add_row(str(i), t.title, t.artist, t.album or "", fmt_time(t.duration), key=t.id)
        self._render_tabs()

    def refresh_queue(self, s: PlayerState, force: bool = False) -> None:
        sig = (len(s.queue), s.index)
        if sig == self._queue_sig and not force:
            return
        self._queue_sig = sig
        table = self.query_one("#queue", DataTable)
        row = table.cursor_row
        table.clear()
        for i, t in enumerate(s.queue):
            marker = "▶" if i == s.index else str(i + 1)
            style = f"bold {ACCENT}" if i == s.index else ""
            table.add_row(
                Text(marker, style=style),
                Text(t.title, style=style),
                t.artist,
                t.album or "",
                fmt_time(t.duration),
            )
        if s.queue:
            table.move_cursor(row=min(row, len(s.queue) - 1))

    # --- actions ----------------------------------------------------------------------

    def _cursor(self) -> int | None:
        table = self.query_one(f"#{self.pane}", DataTable)
        return table.cursor_row if table.row_count else None

    def action_switch_pane(self) -> None:
        if self.query_one("#filter", Input).has_focus:
            self.focus_table()
            return
        self.pane = "queue" if self.pane == "lib" else "lib"
        self.query_one(ContentSwitcher).current = self.pane
        if self.pane == "queue":
            self.refresh_queue(self.sapp.player_state(), force=True)
        self._render_tabs()
        self.focus_table()

    @on(DataTable.RowSelected)
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        event.stop()
        i = event.cursor_row
        if self.pane == "lib":
            self.sapp.play_tracks(self.filtered, i)
        else:
            self.sapp.play_queue_index(i)

    def action_enqueue(self) -> None:
        i = self._cursor()
        if self.pane == "lib" and i is not None:
            self.sapp.enqueue_tracks([self.filtered[i]])

    def action_edit(self) -> None:
        i = self._cursor()
        if i is None:
            return
        track = self.filtered[i] if self.pane == "lib" else self.sapp.player_state().queue[i]
        self.sapp.edit_track(track)

    def action_delete(self) -> None:
        i = self._cursor()
        if i is None:
            return
        if self.pane == "lib":
            self.sapp.delete_track(self.filtered[i])
        else:
            self.sapp.remove_from_queue([i])

    def action_refresh(self) -> None:
        self.sapp.load_library()

    def action_pause(self) -> None:
        self.sapp.player_action("pause")

    def action_filter(self) -> None:
        f = self.query_one("#filter", Input)
        f.add_class("visible")
        f.focus()

    def action_back(self) -> None:
        f = self.query_one("#filter", Input)
        if f.has_focus:
            f.value = ""
            f.remove_class("visible")
            self.focus_table()
        else:
            self.sapp.set_mode("prompt")

    @on(Input.Changed, "#filter")
    def _filter_changed(self, event: Input.Changed) -> None:
        self.apply_filter(event.value)

    @on(Input.Submitted, "#filter")
    def _filter_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if self.pane != "lib":
            self.action_switch_pane()
        self.focus_table()


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
    row("Media keys", "play/pause/next/prev work system-wide (via mpv)")
    row("Ctrl+Q", "quit")
    return Group(*out)
