"""Rich renderables and small formatting helpers."""

from __future__ import annotations

import unicodedata

from rich import box
from rich.table import Table
from rich.text import Text

from soundable_client.api import Track

DEFAULT_ACCENT = "#d97757"  # warm orange, Claude-Code-ish
_accent = [DEFAULT_ACCENT]


def accent() -> str:
    """Current accent colour (changeable in /settings)."""
    return _accent[0]


def set_accent(color: str) -> None:
    _accent[0] = color


def fmt_time(seconds: float | None) -> str:
    if not seconds or seconds < 0:
        return "0:00"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def fold(text: str) -> str:
    """Lowercase + strip accents, for local filtering."""
    return "".join(
        c for c in unicodedata.normalize("NFKD", text.casefold()) if not unicodedata.combining(c)
    )


def track_table(
    tracks: list[Track],
    *,
    title: str | None = None,
    current: int | None = None,
    start: int = 1,
) -> Table:
    table = Table(
        box=box.SIMPLE_HEAD,
        title=title,
        title_justify="left",
        title_style="bold",
        header_style="dim",
        expand=False,
        pad_edge=False,
    )
    table.add_column("#", justify="right", style="dim", no_wrap=True)
    table.add_column("Title", overflow="ellipsis", max_width=48)
    table.add_column("Artist", style="cyan", overflow="ellipsis", max_width=32)
    table.add_column("Album", style="dim", overflow="ellipsis", max_width=28)
    table.add_column("Time", justify="right", style="dim", no_wrap=True)
    for i, t in enumerate(tracks):
        is_current = current is not None and i == current
        num = Text("▶", style=f"bold {accent()}") if is_current else Text(str(start + i))
        table.add_row(
            num,
            Text(t.title, style=f"bold {accent()}" if is_current else ""),
            t.artist,
            t.album or "",
            fmt_time(t.duration),
        )
    return table


def progress_bar(fraction: float, width: int, *, color: str | None = None) -> Text:
    width = max(width, 4)
    fraction = min(max(fraction, 0.0), 1.0)
    filled = round(fraction * width)
    bar = Text("━" * filled, style=color or accent())
    bar.append("─" * (width - filled), style="grey35")
    return bar
