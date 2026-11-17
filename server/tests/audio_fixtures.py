"""Generate small real audio files with ffmpeg for tests."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

HAS_FFMPEG = shutil.which("ffmpeg") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")


def make_audio(
    path: Path,
    *,
    seconds: float = 3.0,
    freq: int = 440,
    codec_args: tuple[str, ...] = (),
    tags: dict[str, str] | None = None,
) -> Path:
    meta: list[str] = []
    for k, v in (tags or {}).items():
        meta += ["-metadata", f"{k}={v}"]
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={freq}:duration={seconds}",
            *codec_args,
            *meta,
            str(path),
        ],
        check=True,
    )
    return path
