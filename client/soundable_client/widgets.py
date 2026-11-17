"""Custom widgets: status header, now-playing bar, task spinners, prompt input."""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from rich.text import Text
from textual import on
from textual.binding import Binding
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from soundable_client.commands import CommandSpec, match_commands
from soundable_client.formatting import ACCENT, fmt_time, progress_bar
from soundable_client.player import PlayerState

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


class StatusHeader(Static):
    server: reactive[str] = reactive("")
    status: reactive[str] = reactive("connecting")  # online | offline | connecting | logged-out
    mode: reactive[str] = reactive("prompt")

    def render(self) -> Text:
        t = Text()
        t.append(" ✻ ", style=f"bold {ACCENT}")
        t.append("soundable", style="bold")
        if self.server:
            t.append("  ·  ", style="dim")
            t.append(self.server, style="dim")
        t.append("  ")
        dot, label, style = {
            "online": ("●", "connected", "green"),
            "offline": ("○", "offline, retrying", "red"),
            "logged-out": ("○", "not logged in", "yellow"),
        }.get(self.status, ("◌", "connecting", "dim"))
        t.append(f"{dot} {label}", style=style)
        mode = f" {self.mode.upper()} "
        pad = max(1, self.size.width - t.cell_len - len(mode) - 1)
        t.append(" " * pad)
        t.append(mode, style=f"bold reverse {ACCENT}")
        return t


class NowPlaying(Widget):
    """Two lines: track / meta, then elapsed ━━━── duration."""

    DEFAULT_CSS = "NowPlaying { height: 2; padding: 0 1; }"

    def __init__(self, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self.state: PlayerState | None = None
        self.available = True

    def show(self, state: PlayerState) -> None:
        self.state = state
        self.refresh()

    def render(self) -> Text:
        width = max(self.size.width - 2, 20)
        s = self.state
        if not self.available:
            return Text("mpv not found: playback disabled (see /help)", style="yellow")
        if s is None or s.current is None:
            line = Text("■ nothing playing", style="dim")
            if s and s.queue:
                line.append(f"  ·  queue {len(s.queue)}", style="dim")
            line.append("\n")
            line.append_text(progress_bar(0, width, color="grey35"))
            return line
        t = s.current
        icon = "⏸" if s.paused else "▶"
        right = Text(f"vol {s.volume}%  ·  {s.index + 1}/{len(s.queue)}", style="dim")
        left = Text()
        left.append(f"{icon} ", style=f"bold {ACCENT}")
        left.append(t.title, style="bold")
        left.append(" — ", style="dim")
        left.append(t.artist, style="cyan")
        max_left = width - right.cell_len - 2
        if left.cell_len > max_left:
            left.truncate(max_left, overflow="ellipsis")
        line = left
        line.append(" " * max(1, width - left.cell_len - right.cell_len))
        line.append_text(right)
        line.append("\n")
        dur = s.duration or t.duration
        elapsed, total = fmt_time(s.position), fmt_time(dur)
        bar_w = width - len(elapsed) - len(total) - 2
        line.append(elapsed + " ", style="dim")
        line.append_text(progress_bar(s.position / dur if dur else 0, bar_w))
        line.append(" " + total, style="dim")
        return line


@dataclass
class _Task:
    label: str
    progress: float | None = None  # 0..1, None = indeterminate


class TaskList(Static):
    """Spinner lines for running background tasks (uploads, imports...)."""

    DEFAULT_CSS = "TaskList { height: auto; padding: 0 1; display: none; }"

    def __init__(self, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self._tasks: dict[int, _Task] = {}
        self._ids = itertools.count()
        self._frame = 0

    def on_mount(self) -> None:
        self.set_interval(0.1, self._tick)

    def _tick(self) -> None:
        if self._tasks:
            self._frame += 1
            self.refresh()

    def add(self, label: str) -> int:
        tid = next(self._ids)
        self._tasks[tid] = _Task(label)
        self.display = True
        self.refresh()
        return tid

    def update_task(
        self, tid: int, *, label: str | None = None, progress: float | None = None
    ) -> None:
        task = self._tasks.get(tid)
        if task:
            if label is not None:
                task.label = label
            if progress is not None:
                task.progress = progress

    def done(self, tid: int) -> None:
        self._tasks.pop(tid, None)
        if not self._tasks:
            self.display = False
        self.refresh()

    def render(self) -> Text:
        out = Text()
        spin = SPINNER[self._frame % len(SPINNER)]
        for i, task in enumerate(self._tasks.values()):
            if i:
                out.append("\n")
            out.append(f"{spin} ", style=f"bold {ACCENT}")
            out.append(task.label)
            if task.progress is not None:
                out.append("  ")
                out.append_text(progress_bar(task.progress, 20))
                out.append(f" {task.progress * 100:3.0f}%", style="dim")
        return out


class CommandMenu(OptionList):
    """Completion menu shown above the prompt while typing a slash command."""

    DEFAULT_CSS = """
    CommandMenu {
        display: none;
        height: auto;
        max-height: 10;
        margin: 0 1;
        border: none;
        background: $surface;
    }
    CommandMenu.visible { display: block; }
    """

    def __init__(self, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self.can_focus = False
        self.specs: list[CommandSpec] = []

    def show_specs(self, specs: list[CommandSpec]) -> None:
        self.specs = specs
        self.clear_options()
        width = max(len(c.name) for c in specs) + 2 if specs else 0
        for c in specs:
            t = Text()
            t.append(c.name.ljust(width), style=f"bold {ACCENT}")
            t.append((c.args + "  ") if c.args else "", style="cyan")
            t.append(c.help, style="dim")
            self.add_option(Option(t))
        self.highlighted = 0 if specs else None
        self.set_class(bool(specs), "visible")

    def hide(self) -> None:
        self.specs = []
        self.remove_class("visible")

    @property
    def is_open(self) -> bool:
        return self.has_class("visible") and bool(self.specs)

    @property
    def selected(self) -> CommandSpec | None:
        if self.is_open and self.highlighted is not None:
            return self.specs[self.highlighted]
        return None

    def move(self, step: int) -> None:
        if self.specs:
            cur = self.highlighted or 0
            self.highlighted = (cur + step) % len(self.specs)


class PromptInput(Input):
    """Input with shell-like history (↑/↓) and a slash-command completion menu."""

    BINDINGS = [
        Binding("up", "up", show=False),
        Binding("down", "down", show=False),
        Binding("tab", "complete", show=False),
        Binding("enter", "enter", show=False, priority=True),
        Binding("escape", "escape", show=False),
    ]

    def __init__(self, history: list[str], **kw: object) -> None:
        super().__init__(
            placeholder="Paste a link, drop files, search, or type / for commands",
            **kw,  # type: ignore[arg-type]
        )
        self.history = history
        self._hist_pos = len(history)
        self._draft = ""
        self._menu_dismissed_for: str | None = None

    @property
    def menu(self) -> CommandMenu:
        return self.app.query_one(CommandMenu)

    def remember(self, line: str) -> None:
        if line and (not self.history or self.history[-1] != line):
            self.history.append(line)
            del self.history[:-200]
        self._hist_pos = len(self.history)

    @on(Input.Changed)
    def _update_menu(self, event: Input.Changed) -> None:
        if event.value == self._menu_dismissed_for:
            return
        self._menu_dismissed_for = None
        specs = match_commands(event.value)
        fully_typed = len(specs) == 1 and specs[0].name == event.value and not specs[0].args
        if specs and not fully_typed:
            self.menu.show_specs(specs)
        else:
            self.menu.hide()

    def _set(self, value: str) -> None:
        self.value = value
        self.cursor_position = len(value)

    def action_up(self) -> None:
        if self.menu.is_open:
            self.menu.move(-1)
        else:
            self._history(-1)

    def action_down(self) -> None:
        if self.menu.is_open:
            self.menu.move(1)
        else:
            self._history(1)

    def _history(self, step: int) -> None:
        if not self.history:
            return
        if self._hist_pos == len(self.history):
            self._draft = self.value
        self._hist_pos = max(0, min(len(self.history), self._hist_pos + step))
        self._set(
            self._draft if self._hist_pos == len(self.history) else self.history[self._hist_pos]
        )
        self.menu.hide()

    def _accept(self) -> CommandSpec | None:
        spec = self.menu.selected
        if spec is None:
            return None
        self.menu.hide()
        new = spec.name + (" " if spec.args else "")
        self._menu_dismissed_for = new
        self._set(new)
        return spec

    def action_complete(self) -> None:
        self._accept()

    async def action_enter(self) -> None:
        spec = self.menu.selected
        if spec is not None and self.value.strip() != spec.name:
            self._accept()
            if spec.args:
                return  # completed "/pl" -> "/play ": now wait for the arguments
        self.menu.hide()
        await self.action_submit()

    def action_escape(self) -> None:
        """Esc closes the menu, then clears the input, then switches to listening mode."""
        if self.menu.is_open:
            self._menu_dismissed_for = self.value
            self.menu.hide()
        elif self.value:
            self._set("")
        else:
            self.app.call_later(self.app.run_action, "set_mode('listen')")
