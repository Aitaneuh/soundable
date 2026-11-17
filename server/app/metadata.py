"""Title/artist cleanup shared by uploads and URL imports."""

from __future__ import annotations

import re

# Noise commonly appended to YouTube titles / file names. Matched inside () or [].
_NOISE_WORDS = (
    r"official\s*(music\s*)?(video|audio|visuali[sz]er|lyric\s*video|clip)",
    r"(official\s*)?lyrics?(\s*video)?",
    r"audio(\s*officiel)?",
    r"clip\s*officiel",
    r"visuali[sz]er",
    r"hd|hq|4k|1080p|720p",
    r"video\s*officielle?",
    r"music\s*video",
    r"explicit",
    r"(4k\s+|hd\s+)?remaster(ed)?(\s*\d{4})?|\d{4}\s*remaster(ed)?|4k\s+version",
)
_NOISE_RE = re.compile(r"\s*[\(\[]\s*(?:" + "|".join(_NOISE_WORDS) + r")\s*[\)\]]", re.IGNORECASE)
_SPLIT_RE = re.compile(r"\s+[-–—|]\s+")  # noqa: RUF001 (hyphen, en dash, em dash, pipe)
_TOPIC_SUFFIX = re.compile(r"\s*-\s*Topic$")
_VEVO_SUFFIX = re.compile(r"VEVO$")


def clean_title(title: str) -> str:
    cleaned = _NOISE_RE.sub("", title)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" -_")
    return cleaned or title.strip()


def clean_artist(artist: str) -> str:
    """'Daft Punk - Topic' / 'DaftPunkVEVO' channel names -> artist name."""
    a = _TOPIC_SUFFIX.sub("", artist.strip())
    if a.endswith("VEVO") and len(a) > 4:
        a = _VEVO_SUFFIX.sub("", a)
    return a.strip() or artist.strip()


def split_artist_title(text: str) -> tuple[str | None, str]:
    """'Artist - Title' -> ('Artist', 'Title'); otherwise (None, text)."""
    parts = _SPLIT_RE.split(text, maxsplit=1)
    if len(parts) == 2 and all(p.strip() for p in parts):
        return parts[0].strip(), parts[1].strip()
    return None, text.strip()


def guess_from_filename(stem: str) -> tuple[str | None, str]:
    """Best-effort artist/title from a file name without extension."""
    stem = re.sub(r"^\d{1,3}[\s._-]+", "", stem)  # leading track number "01 - "
    stem = stem.replace("_", " ")
    artist, title = split_artist_title(stem)
    return artist, clean_title(title)
