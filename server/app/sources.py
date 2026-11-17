"""Import URL allowlist and normalisation.

Only YouTube and SoundCloud track URLs are accepted. The URL handed to yt-dlp is always
rebuilt from validated parts (never the raw user input), and yt-dlp itself is restricted
to the YouTube and SoundCloud extractors, so the server cannot be used as a generic
downloader / SSRF proxy.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

YOUTUBE_HOSTS = frozenset(
    {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}
)
SOUNDCLOUD_HOSTS = frozenset({"soundcloud.com", "www.soundcloud.com", "m.soundcloud.com"})
SOUNDCLOUD_SHORT_HOST = "on.soundcloud.com"

_YT_ID = re.compile(r"[A-Za-z0-9_-]{11}")
_SC_SEGMENT = re.compile(r"[A-Za-z0-9_-]{1,255}")
# soundcloud.com/<user>/<this> paths that are not tracks
_SC_RESERVED = frozenset(
    {
        "sets",
        "likes",
        "tracks",
        "albums",
        "reposts",
        "followers",
        "following",
        "popular-tracks",
        "comments",
        "spotlight",
        "toptracks",
    }
)


class UnsupportedUrl(ValueError):
    pass


@dataclass(frozen=True)
class ImportUrl:
    source: str  # "youtube" | "soundcloud"
    url: str  # canonical URL given to yt-dlp
    # Duplicate-detection key when knowable from the URL alone (YouTube video id).
    # SoundCloud keys use the numeric track id, only known after extraction.
    key: str | None
    needs_resolve: bool = False  # on.soundcloud.com short link


def youtube_key(video_id: str) -> str:
    return f"youtube:{video_id}"


def soundcloud_key(track_id: str | int) -> str:
    return f"soundcloud:{track_id}"


def parse_import_url(raw: str) -> ImportUrl:
    text = raw.strip()
    if not text or len(text) > 2000 or any(c.isspace() for c in text):
        raise UnsupportedUrl("Paste a single YouTube or SoundCloud link.")
    if "://" not in text:
        text = "https://" + text
    p = urlparse(text)
    host = (p.hostname or "").lower()
    if p.scheme not in {"http", "https"} or p.username or p.password or p.port not in (None, 443):
        raise UnsupportedUrl("Only plain https YouTube/SoundCloud links are supported.")

    if host in YOUTUBE_HOSTS:
        return _youtube(host, p.path, parse_qs(p.query))
    if host in SOUNDCLOUD_HOSTS:
        return _soundcloud(p.path)
    if host == SOUNDCLOUD_SHORT_HOST:
        slug = p.path.strip("/")
        if not _SC_SEGMENT.fullmatch(slug):
            raise UnsupportedUrl("That on.soundcloud.com link looks malformed.")
        return ImportUrl("soundcloud", f"https://{SOUNDCLOUD_SHORT_HOST}/{slug}", None, True)
    raise UnsupportedUrl("Only YouTube and SoundCloud links can be imported.")


def _youtube(host: str, path: str, qs: dict[str, list[str]]) -> ImportUrl:
    video_id: str | None = None
    parts = [s for s in path.split("/") if s]
    if host == "youtu.be":
        video_id = parts[0] if parts else None
    elif parts[:1] == ["watch"] or not parts:
        video_id = (qs.get("v") or [None])[0]
    elif len(parts) >= 2 and parts[0] in {"shorts", "live", "embed", "v"}:
        video_id = parts[1]
    elif parts[:1] == ["playlist"]:
        raise UnsupportedUrl(
            "Playlist imports aren't supported yet: paste a link to a single video."
        )
    if not video_id and "list" in qs:
        raise UnsupportedUrl(
            "Playlist imports aren't supported yet: paste a link to a single video."
        )
    if not video_id or not _YT_ID.fullmatch(video_id):
        raise UnsupportedUrl("That doesn't look like a YouTube video link.")
    # A watch?v=X&list=Y link imports just the video X.
    return ImportUrl(
        "youtube", f"https://www.youtube.com/watch?v={video_id}", youtube_key(video_id)
    )


def _soundcloud(path: str) -> ImportUrl:
    parts = [s for s in path.split("/") if s]
    if len(parts) >= 2 and parts[1] == "sets":
        raise UnsupportedUrl("SoundCloud sets/playlists aren't supported yet: paste a track link.")
    secret = len(parts) == 3 and parts[2].startswith("s-")
    if not (len(parts) == 2 or secret) or not all(_SC_SEGMENT.fullmatch(s) for s in parts):
        raise UnsupportedUrl("That doesn't look like a SoundCloud track link.")
    if parts[1] in _SC_RESERVED or parts[0] in {"discover", "search", "you", "stream"}:
        raise UnsupportedUrl("That's a SoundCloud page, not a track: paste a track link.")
    return ImportUrl("soundcloud", "https://soundcloud.com/" + "/".join(parts), None)


def resolve_soundcloud_short(url: str, timeout: float = 15.0) -> ImportUrl:
    """Follow exactly one redirect of an on.soundcloud.com link and validate the target."""

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "Mozilla/5.0"})
    try:
        opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code in (301, 302, 303, 307, 308) and e.headers.get("Location"):
            target = parse_import_url(e.headers["Location"])
            if target.source != "soundcloud" or target.needs_resolve:
                raise UnsupportedUrl("That short link doesn't point to a SoundCloud track.") from e
            return target
        raise UnsupportedUrl(f"SoundCloud short link failed (HTTP {e.code}).") from e
    raise UnsupportedUrl("That SoundCloud short link didn't redirect anywhere.")
