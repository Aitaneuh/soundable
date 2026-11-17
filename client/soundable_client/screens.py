"""Modal screens: login, edit track, confirmation."""

from __future__ import annotations

import platform

from rich.text import Text
from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from soundable_client.api import Api, ApiError, AuthError, NetworkError, Playlist, Track
from soundable_client.config import InvalidServerUrl, normalize_server_url
from soundable_client.formatting import ACCENT


class LoginResult:
    def __init__(self, server_url: str, token: str) -> None:
        self.server_url = server_url
        self.token = token


class LoginScreen(ModalScreen[LoginResult | None]):
    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, server_url: str = "", *, can_cancel: bool = True) -> None:
        super().__init__()
        self.initial_url = server_url
        self.can_cancel = can_cancel

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(f"[bold {ACCENT}]✻[/] [bold]Log in to your soundable server[/]")
            yield Label("Server URL", classes="field-label")
            yield Input(
                self.initial_url,
                placeholder="https://soundable.example.com",
                id="url",
            )
            yield Label("Password", classes="field-label")
            yield Input(placeholder="admin password", password=True, id="password")
            yield Static("", id="login-status")
            with Horizontal(classes="buttons"):
                if self.can_cancel:
                    yield Button("Cancel", id="cancel")
                yield Button("Log in", variant="primary", id="login")

    def on_mount(self) -> None:
        self.query_one("#password" if self.initial_url else "#url", Input).focus()

    def _status(self, msg: str, style: str = "red") -> None:
        self.query_one("#login-status", Static).update(f"[{style}]{msg}[/]")

    @on(Input.Submitted, "#url")
    def _url_done(self) -> None:
        self.query_one("#password", Input).focus()

    @on(Input.Submitted, "#password")
    @on(Button.Pressed, "#login")
    def _submit(self) -> None:
        self.attempt_login()

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        if self.can_cancel:
            self.dismiss(None)

    @work(exclusive=True)
    async def attempt_login(self) -> None:
        raw_url = self.query_one("#url", Input).value
        password = self.query_one("#password", Input).value
        try:
            url = normalize_server_url(raw_url)
        except InvalidServerUrl as e:
            self._status(str(e))
            self.query_one("#url", Input).focus()
            return
        if not password:
            self._status("enter the password")
            return
        self._status("connecting…", "dim")
        api = Api(url)
        try:
            if not await api.health():
                self._status(f"no soundable server answering at {url}")
                return
            token = await api.login(password, name=f"terminal ({platform.node()})")
        except AuthError:
            self._status("wrong password")
            self.query_one("#password", Input).value = ""
            return
        except NetworkError as e:
            self._status(str(e))
            return
        except ApiError as e:
            self._status(f"login failed: {e}")
            return
        finally:
            await api.aclose()
        self.dismiss(LoginResult(url, token))


class EditTrackScreen(ModalScreen[dict[str, str | None] | None]):
    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, track: Track) -> None:
        super().__init__()
        self.track = track

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static("[bold]Edit track[/]")
            yield Label("Title", classes="field-label")
            yield Input(self.track.title, id="title")
            yield Label("Artist", classes="field-label")
            yield Input(self.track.artist, id="artist")
            yield Label("Album (empty = none)", classes="field-label")
            yield Input(self.track.album or "", id="album")
            yield Static("", id="edit-status")
            with Horizontal(classes="buttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Save", variant="primary", id="save")

    @on(Input.Submitted)
    @on(Button.Pressed, "#save")
    def _save(self) -> None:
        title = self.query_one("#title", Input).value.strip()
        artist = self.query_one("#artist", Input).value.strip()
        album = self.query_one("#album", Input).value.strip()
        if not title or not artist:
            self.query_one("#edit-status", Static).update("[red]title and artist are required[/]")
            return
        changes: dict[str, str | None] = {}
        if title != self.track.title:
            changes["title"] = title
        if artist != self.track.artist:
            changes["artist"] = artist
        if album != (self.track.album or ""):
            changes["album"] = album
        self.dismiss(changes)

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("y", "answer(True)", show=False),
        Binding("n,escape", "answer(False)", show=False),
    ]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(self.question)
            yield Static("[dim]y = yes · n / Esc = no[/]")
            with Horizontal(classes="buttons"):
                yield Button("No", id="no")
                yield Button("Yes", variant="error", id="yes")

    def on_mount(self) -> None:
        self.query_one("#no", Button).focus()

    def action_answer(self, value: bool) -> None:
        self.dismiss(value)

    @on(Button.Pressed)
    def _pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")


class NameScreen(ModalScreen[str | None]):
    """Ask for a single line of text (e.g. a playlist name)."""

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, title: str, initial: str = "", placeholder: str = "") -> None:
        super().__init__()
        self.title_text = title
        self.initial = initial
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(f"[bold]{self.title_text}[/]")
            yield Input(self.initial, placeholder=self.placeholder, id="name")
            yield Static("[dim]Enter to confirm · Esc to cancel[/]")

    def on_mount(self) -> None:
        self.query_one("#name", Input).focus()

    @on(Input.Submitted)
    def _done(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        if value:
            self.dismiss(value)

    def action_cancel(self) -> None:
        self.dismiss(None)


NEW_PLAYLIST = "__new__"


class PlaylistPickerScreen(ModalScreen[str | None]):
    """Choose a playlist (returns its id), or NEW_PLAYLIST to create one."""

    BINDINGS = [Binding("escape", "cancel", show=False)]

    def __init__(self, playlists: list[Playlist], what: str) -> None:
        super().__init__()
        self.playlists = playlists
        self.what = what

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Static(f"Add [bold]{self.what}[/] to…")
            options = [Option(Text("+ New playlist…", style=f"bold {ACCENT}"), id=NEW_PLAYLIST)]
            options += [
                Option(Text.assemble(p.name, (f"  {p.track_count} tracks", "dim")), id=p.id)
                for p in self.playlists
            ]
            yield OptionList(*options, id="picker")
            yield Static("[dim]↑/↓ choose · Enter confirm · Esc cancel[/]")

    def on_mount(self) -> None:
        picker = self.query_one("#picker", OptionList)
        picker.focus()
        picker.highlighted = 1 if self.playlists else 0

    @on(OptionList.OptionSelected)
    def _chosen(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)
