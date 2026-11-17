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
