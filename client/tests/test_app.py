from __future__ import annotations

import pytest

from soundable_client import app as appmod
from soundable_client.config import Config
from soundable_client.screens import LoginScreen


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("SOUNDABLE_CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(appmod, "find_mpv", lambda configured="": None)
    monkeypatch.setattr(appmod, "load_token", lambda url: None)


async def test_first_run_shows_login(isolated) -> None:
    app = appmod.SoundableApp(Config())
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        assert isinstance(app.screen, LoginScreen)
        await pilot.press("escape")  # can't cancel on first run
        assert isinstance(app.screen, LoginScreen)


async def test_modes_and_unknown_command(isolated) -> None:
    app = appmod.SoundableApp(Config(server_url="http://localhost:1"))
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        if isinstance(app.screen, LoginScreen):  # no token -> login dialog; dismiss it
            app.pop_screen()
        await pilot.pause(0.1)
        await pilot.press("escape")
        assert app.mode == "listen"
        await pilot.press("x")  # typing jumps back to the prompt with the character
        assert app.mode == "prompt"
        assert app.prompt.value == "x"


def test_number_parsing() -> None:
    app = appmod.SoundableApp(Config())
    assert app._numbers("3", 5) == [2]
    assert app._numbers("1 3-4", 5) == [0, 2, 3]
    assert app._numbers("6", 5) is None
    assert app._numbers("abc", 5) is None


async def _ready_app(pilot, app) -> None:  # type: ignore[no-untyped-def]
    await pilot.pause(0.3)
    if isinstance(app.screen, LoginScreen):
        app.pop_screen()
    await pilot.pause(0.1)


async def test_command_menu(isolated) -> None:
    from soundable_client.widgets import CommandMenu

    app = appmod.SoundableApp(Config(server_url="http://localhost:1"))
    async with app.run_test() as pilot:
        await _ready_app(pilot, app)
        menu = app.query_one(CommandMenu)
        await pilot.press("slash")
        assert menu.is_open and len(menu.specs) > 10
        await pilot.press("q", "u")
        assert [c.name for c in menu.specs] == [
            "/queue",
            "/queue add",
            "/queue rm",
            "/queue clear",
            "/quit",
        ]
        await pilot.press("down", "tab")  # pick "/queue add" -> takes args
        assert app.prompt.value == "/queue add "
        assert not menu.is_open
        app.prompt.value = ""
        await pilot.press("slash", "p", "l")
        await pilot.press("enter")  # completes but does not submit (needs args)
        assert app.prompt.value == "/play "
        app.prompt.value = ""
        await pilot.press("slash", "x", "y", "z")
        assert not menu.is_open
        await pilot.press("backspace", "backspace", "backspace", "h")
        assert menu.is_open
        await pilot.press("escape")
        assert not menu.is_open and app.prompt.value == "/h"


async def test_space_pauses_in_browse(isolated, monkeypatch) -> None:
    app = appmod.SoundableApp(Config(server_url="http://localhost:1"))
    calls: list[str] = []
    monkeypatch.setattr(app, "player_action", calls.append)
    monkeypatch.setattr(app, "load_library", lambda: None)
    async with app.run_test() as pilot:
        await _ready_app(pilot, app)
        await pilot.press("ctrl+b")
        assert app.mode == "browse"
        await pilot.press("space")
        assert calls == ["pause"]


def test_split_playlist_ref_and_numbers() -> None:
    app = appmod.SoundableApp(Config())
    assert app._split_ref_numbers("chill vibes 1 3 5-7") == ("chill vibes", "1 3 5-7")
    assert app._split_ref_numbers("road") == ("road", "")
    assert app._split_ref_numbers("2 4") == ("2", "4")  # playlist #2, track 4
    assert app._split_ref_numbers("top 50 3") == ("top", "50 3")  # numeric-ending names: ambiguous


def test_playlist_commands_in_menu() -> None:
    from soundable_client.commands import match_commands

    names = [c.name for c in match_commands("/playlist ")]
    assert "/playlist play" in names and "/playlist save" in names


def test_key_validation() -> None:
    from soundable_client import keys

    keys.set_overrides({})
    assert keys.validate("app.browse", "x") is not None  # global needs a modifier
    assert keys.validate("app.browse", "ctrl+l") is None
    assert keys.validate("app.browse", "f5") is None
    assert "already used" in (keys.validate("app.browse", "ctrl+s") or "")
    assert "reserved" in (keys.validate("listen.next", "escape") or "")
    assert keys.validate("listen.next", "l") is None
    assert "already used" in (keys.validate("listen.next", "s") or "")  # listen.shuffle
    assert keys.validate("browse.enqueue", "l") is None  # other scope: fine
    assert keys.display_key("ctrl+b") == "Ctrl+B" and keys.show("listen.back") == "Shift+← / ,"


async def test_remapped_key_and_settings_screen(isolated, monkeypatch) -> None:
    from soundable_client.settings import KeyCaptureScreen, SettingsScreen

    cfg = Config(server_url="http://localhost:1")
    cfg.keymap = {"listen.next": "l"}
    app = appmod.SoundableApp(cfg)
    calls: list[str] = []
    monkeypatch.setattr(app, "player_action", calls.append)
    async with app.run_test() as pilot:
        await _ready_app(pilot, app)
        await pilot.press("escape")  # listening mode
        assert app.mode == "listen"
        await pilot.press("l")
        assert calls == ["next"]  # remapped key works...
        await pilot.press("right")
        assert calls == ["next"]  # ...and the old one no longer does
        assert app.mode == "listen"

        await pilot.press("f2")
        assert isinstance(app.screen, SettingsScreen)
        app.update_setting("theme", "nord")
        assert app.theme == "nord" and cfg.theme == "nord"

        # capture a new key for "Next track" through the dialog
        app.screen.query_one("#keys").focus()
        app.screen.query_one("#keys").move_cursor(row=6)  # listen.next
        await pilot.press("enter")
        assert isinstance(app.screen, KeyCaptureScreen)
        await pilot.press("s")  # conflicts with shuffle -> refused, dialog stays
        assert isinstance(app.screen, KeyCaptureScreen)
        await pilot.press("k")
        await pilot.pause(0.1)
        assert cfg.keymap["listen.next"] == "k"
        await pilot.press("escape")
        assert not isinstance(app.screen, SettingsScreen)
