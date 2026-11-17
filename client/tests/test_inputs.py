from __future__ import annotations

import sys
from pathlib import Path

import pytest

from soundable_client.config import InvalidServerUrl, normalize_server_url
from soundable_client.inputs import classify, is_import_url, split_paths
from soundable_client.mpv import escape_option_value


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "music.youtube.com/watch?v=abc",
        "https://soundcloud.com/artist/track",
        "https://on.soundcloud.com/xyz",
    ],
)
def test_import_urls(url: str) -> None:
    assert is_import_url(url)
    assert classify(url).kind == "import"


@pytest.mark.parametrize("text", ["https://evil.com/youtube.com", "youtube", "notyoutube.com/x"])
def test_not_import_urls(text: str) -> None:
    assert not is_import_url(text)


def test_classify_paths(tmp_path: Path) -> None:
    a = tmp_path / "with space.mp3"
    b = tmp_path / "b.flac"
    a.write_bytes(b"x")
    b.write_bytes(b"x")
    c = classify(f'"{a}" "{b}"')
    assert c.kind == "upload"
    assert c.paths == (a, b)
    assert classify(str(b)).kind == "upload"
    # one missing file -> not treated as paths
    assert classify(f'"{a}" "{tmp_path / "missing.mp3"}"').kind == "search"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows drag-and-drop format")
def test_split_windows_paths() -> None:
    assert split_paths(r'"C:\My Music\a b.mp3" C:\x.mp3') == [r"C:\My Music\a b.mp3", r"C:\x.mp3"]


def test_classify_other() -> None:
    assert classify("  ").kind == "empty"
    assert classify("/play 3").kind == "command"
    assert classify("daft punk").kind == "search"


def test_server_url_validation() -> None:
    assert normalize_server_url("soundable.example.com/") == "https://soundable.example.com"
    assert normalize_server_url("localhost:8000") == "http://localhost:8000"
    assert normalize_server_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000"
    with pytest.raises(InvalidServerUrl):
        normalize_server_url("http://soundable.example.com")
    with pytest.raises(InvalidServerUrl):
        normalize_server_url("https://x.com/?a=1")


def test_mpv_option_escape() -> None:
    assert escape_option_value("a,b") == "%3%a,b"
    assert escape_option_value("é") == "%2%é"


def test_html_error_pages_are_explained() -> None:
    import httpx

    from soundable_client.api import _detail

    msg, _ = _detail(httpx.Response(403, headers={"content-type": "text/html"}, text="<html>x"))
    assert "web page" in msg and "403" in msg
    msg, data = _detail(
        httpx.Response(409, json={"detail": "Already there", "existing_track_id": "1"})
    )
    assert msg == "Already there" and data["existing_track_id"] == "1"
