"""/settings screen (also F2 by default). Every change applies immediately and is saved."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rich.text import Text
from textual import events, on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Button, DataTable, Label, Select, Static, Switch

from soundable_client import keys
from soundable_client.formatting import DEFAULT_ACCENT, accent

if TYPE_CHECKING:
    from soundable_client.app import SoundableApp

ACCENTS = [
    ("Claude orange", DEFAULT_ACCENT),
    ("Sky blue", "#5fafff"),
    ("Mint green", "#5fd787"),
    ("Lavender", "#af87ff"),
    ("Pink", "#ff87af"),
    ("Gold", "#ffd75f"),
    ("Coral red", "#ff6b6b"),
    ("Teal", "#4fd1c5"),
]
SCOPE_LABEL = {"global": "everywhere", "listen": "listening mode", "browse": "browse mode"}


def _swatch(name: str, color: str) -> Text:
    return Text.assemble(("██ ", color), name)


class KeyCaptureScreen(ModalScreen[str | None]):
    """Press the new key for an action (Esc cancels)."""

    def __init__(self, action: keys.KeyAction) -> None:
        super().__init__()
        self.action_def = action

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(f"Press the new key for [bold]{self.action_def.label}[/]")
            yield Static(f"[dim]current: {keys.show(self.action_def.id)} · Esc to cancel[/]")
            yield Static("", id="capture-error")

    def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        if event.key == "escape":
            self.dismiss(None)
            return
        if error := keys.validate(self.action_def.id, event.key):
            self.query_one("#capture-error", Static).update(f"[red]{error}[/]")
            return
        self.dismiss(event.key)


class SettingsScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "close", "close", show=False),
        Binding("x", "reset_key", "reset key", show=False),
    ]

    DEFAULT_CSS = """
    SettingsScreen { background: $background; }
    SettingsScreen #settings-title { height: 1; padding: 0 2; background: $panel; }
    SettingsScreen VerticalScroll { padding: 1 2; }
    SettingsScreen .section { margin-top: 1; text-style: bold; color: $soundable-accent; }
    SettingsScreen .row { height: auto; }
    SettingsScreen .row Label { width: 30; padding-top: 1; }
    SettingsScreen .row Select { width: 46; }
    SettingsScreen .row Switch { width: auto; }
    SettingsScreen .help { color: $text-muted; padding: 1 0 0 2; width: 1fr; }
    SettingsScreen #keys { height: auto; max-height: 30; margin-bottom: 1; }
    SettingsScreen #settings-footer { height: 1; padding: 0 2; color: $text-muted; }
    """

    @property
    def sapp(self) -> SoundableApp:
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        c = self.sapp.config
        yield Static(
            Text.assemble((" ✻ ", f"bold {accent()}"), ("Settings", "bold")), id="settings-title"
        )
        with VerticalScroll():
            yield Label("Appearance", classes="section")
            yield self._row(
                "Theme",
                Select(
                    [(t, t) for t in sorted(self.app.available_themes)],
                    value=c.theme,
                    allow_blank=False,
                    id="theme",
                ),
                "previewed live",
            )
            accents = list(ACCENTS)
            if c.accent not in {v for _, v in accents}:
                accents.append(("Custom", c.accent))
            yield self._row(
                "Accent colour",
                Select(
                    [(_swatch(n, v), v) for n, v in accents],
                    value=c.accent,
                    allow_blank=False,
                    id="accent",
                ),
            )

            yield Label("Playback", classes="section")
            yield self._row(
                "Resume last queue on start",
                Switch(value=c.restore_queue, id="restore_queue"),
                "reloads what was playing, paused where you left it",
            )
            yield self._row(
                "Loudness normalisation",
                Switch(value=c.normalize, id="normalize"),
                "evens out quiet and loud tracks (EBU R128)",
            )
            yield self._row(
                "Audio output",
                Select([("Automatic", "auto")], value="auto", allow_blank=False, id="audio_device"),
            )
            yield self._row(
                "Volume step",
                Select(
                    [(f"{v} %", v) for v in (1, 2, 5, 10)],
                    value=c.volume_step,
                    allow_blank=False,
                    id="volume_step",
                ),
            )
            yield self._row(
                "Seek step",
                Select(
                    [(f"{v} s", v) for v in (5, 10, 15, 30, 60)],
                    value=c.seek_step,
                    allow_blank=False,
                    id="seek_step",
                ),
            )

            yield Label("Behaviour", classes="section")
            yield self._row(
                "Start in",
                Select(
                    [("Prompt", "prompt"), ("Listening mode", "listen"), ("Browse mode", "browse")],
                    value=c.start_mode,
                    allow_blank=False,
                    id="start_mode",
                ),
            )
            yield self._row(
                "Library order (browse mode)",
                Select(
                    [("Artist", "artist"), ("Title", "title"), ("Recently added", "recent")],
                    value=c.library_sort,
                    allow_blank=False,
                    id="library_sort",
                ),
            )

            yield Label("Keys", classes="section")
            yield Static("[dim]Enter: change the key · x: reset it to the default[/]")
            yield DataTable(id="keys", cursor_type="row", zebra_stripes=True)
            yield Button("Reset all keys to defaults", id="reset_keys")
        yield Static("Esc to close · changes are saved automatically", id="settings-footer")

    def _row(self, label: str, widget: Any, help_text: str = "") -> Horizontal:
        children = [Label(label), widget]
        if help_text:
            children.append(Static(help_text, classes="help"))
        return Horizontal(*children, classes="row")

    def on_mount(self) -> None:
        table = self.query_one("#keys", DataTable)
        table.add_column("Action", width=32)
        table.add_column("Key", width=26)
        table.add_column("Where", width=18)
        self._fill_keys()
        self._load_devices()

    def _fill_keys(self) -> None:
        table = self.query_one("#keys", DataTable)
        row = table.cursor_row
        table.clear()
        for a in keys.KEY_ACTIONS:
            changed = a.id in self.sapp.config.keymap
            key = Text(keys.show(a.id), style=f"bold {accent()}" if changed else "")
            table.add_row(a.label, key, SCOPE_LABEL[a.scope], key=a.id)
        table.move_cursor(row=row)

    @work
    async def _load_devices(self) -> None:
        player = self.sapp.player
        if player is None:
            return
        try:
            devices = await player.audio_devices()
        except Exception:
            return
        select = self.query_one("#audio_device", Select)
        options = [("Automatic" if n == "auto" else d, n) for n, d in devices]
        with self.prevent(Select.Changed):
            select.set_options(options)
            current = self.sapp.config.audio_device
            select.value = current if current in {n for n, _ in devices} else "auto"

    # --- events -----------------------------------------------------------------------

    @on(Select.Changed)
    def _select_changed(self, event: Select.Changed) -> None:
        if event.select.id and event.value is not Select.BLANK:
            self.sapp.update_setting(event.select.id, event.value)

    @on(Switch.Changed)
    def _switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.id:
            self.sapp.update_setting(event.switch.id, event.value)

    @on(DataTable.RowSelected, "#keys")
    def _change_key(self, event: DataTable.RowSelected) -> None:
        action = keys.BY_ID[str(event.row_key.value)]

        def done(key: str | None) -> None:
            if key:
                self.sapp.set_key(action.id, key)
                self._fill_keys()

        self.app.push_screen(KeyCaptureScreen(action), done)

    def action_reset_key(self) -> None:
        table = self.query_one("#keys", DataTable)
        if not table.has_focus or not table.row_count:
            return
        action_id = str(table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value)
        self.sapp.set_key(action_id, None)
        self._fill_keys()

    @on(Button.Pressed, "#reset_keys")
    def _reset_all(self) -> None:
        self.sapp.reset_keys()
        self._fill_keys()

    def action_close(self) -> None:
        self.app.pop_screen()
