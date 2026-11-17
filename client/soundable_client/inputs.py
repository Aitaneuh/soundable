"""Classifying free-form prompt input: URL import, file paths to upload, or a search."""

from __future__ import annotations

import os
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

AUDIO_EXTENSIONS = frozenset(
    {".mp3", ".m4a", ".mp4", ".aac", ".flac", ".wav", ".ogg", ".oga", ".opus", ".webm"}
)

_IMPORT_HOSTS = (
    "youtube.com",
    "youtu.be",
    "music.youtube.com",
    "m.youtube.com",
    "soundcloud.com",
    "on.soundcloud.com",
    "m.soundcloud.com",
)

# Windows Terminal pastes dropped files as  "C:\a b\x.mp3" "C:\y.mp3"  or bare paths.
_WIN_TOKEN = re.compile(r'"([^"]+)"|\'([^\']+)\'|(\S+)')


def split_paths(text: str) -> list[str]:
    text = text.strip()
    if sys.platform == "win32":
        return [next(g for g in m.groups() if g is not None) for m in _WIN_TOKEN.finditer(text)]
    try:
        return shlex.split(text)  # handles quotes and backslash-escaped spaces
    except ValueError:
        return text.split()


def is_import_url(text: str) -> bool:
    t = text.strip()
    if " " in t:
        return False
    p = urlparse(t if "://" in t else "https://" + t)
    host = (p.hostname or "").lower()
    return p.scheme in {"http", "https"} and any(
        host == h or host.endswith("." + h) for h in _IMPORT_HOSTS
    )


def expand_audio_paths(paths: list[Path]) -> list[Path]:
    """Files are kept as-is; directories are expanded (recursively) to their audio files."""
    out: list[Path] = []
    for p in paths:
        if p.is_dir():
            out += sorted(
                f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in AUDIO_EXTENSIONS
            )
        else:
            out.append(p)
    return out


@dataclass(frozen=True)
class Classified:
    kind: str  # "command" | "import" | "upload" | "search" | "empty"
    value: str = ""
    paths: tuple[Path, ...] = ()


def _uploadable(p: Path) -> bool:
    """An existing file or folder, but never a filesystem/drive root ("/", "C:\\")."""
    try:
        return p.exists() and not (p.is_dir() and p.resolve() == Path(p.resolve().anchor))
    except OSError:
        return False


def classify(text: str) -> Classified:
    t = text.strip()
    if not t:
        return Classified("empty")
    if t.startswith("/"):
        # On macOS/Linux a dropped file path also starts with "/": prefer the path.
        tokens = split_paths(t)
        if tokens and all(_uploadable(Path(os.path.expanduser(tok))) for tok in tokens):
            return Classified("upload", t, tuple(Path(os.path.expanduser(x)) for x in tokens))
        return Classified("command", t)
    if is_import_url(t):
        return Classified("import", t)
    tokens = split_paths(t)
    if tokens:
        candidates = [Path(os.path.expanduser(tok)) for tok in tokens]
        if all(_uploadable(c) for c in candidates):
            return Classified("upload", t, tuple(candidates))
    return Classified("search", t)
