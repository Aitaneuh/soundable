"""Downloading a YouTube/SoundCloud track with yt-dlp (Python API) and turning it into a
library track."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import shutil
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import yt_dlp

from app import jobs
from app.audio import NotAudioError
from app.config import get_settings
from app.db import get_sessionmaker
from app.library import (
    DuplicateTrackError,
    TrackMeta,
    find_duplicate,
    ingest_file,
    sha256_file,
    tmp_dir,
)
from app.metadata import clean_artist, clean_title, split_artist_title
from app.playlist_import import MAX_PLAYLIST_TRACKS, PlaylistInfo
from app.sources import (
    ImportUrl,
    UnsupportedUrl,
    parse_import_url,
    resolve_soundcloud_short,
    soundcloud_key,
    youtube_key,
)

log = logging.getLogger(__name__)

MAX_DURATION_SECONDS = 3 * 3600
MAX_FILESIZE = 1024 * 1024 * 1024

# Prefer AAC in MP4 (YouTube's itag 140): iOS plays it natively, so no lossy->lossy
# conversion. Otherwise take the best audio-only stream; Opus/WebM gets converted to AAC
# by ingest_file.
FORMAT = "bestaudio[ext=m4a]/bestaudio[acodec^=mp3]/bestaudio"


class ImportFailed(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.message = message
        self.retryable = retryable


# (pattern, friendly message, retryable). First match wins.
_ERRORS: list[tuple[str, str, bool]] = [
    (
        r"confirm (you(')?re|you are) not a bot|sign in to confirm(?! your age)",
        "YouTube asked for sign-in / bot check. Provide a cookies file (YTDLP_COOKIES_FILE).",
        False,
    ),
    (
        r"confirm your age|age[- ]restricted|inappropriate for some users",
        "Age-restricted video: it needs a cookies file (YTDLP_COOKIES_FILE).",
        False,
    ),
    (r"private video", "This video is private.", False),
    (
        r"drm[- ]protected|drm protection",
        "DRM-protected (licensed / SoundCloud Go+ track): it can't be downloaded.",
        False,
    ),
    (r"members[- ]only|join this channel", "Members-only video.", False),
    (
        r"premieres in|live event will begin|is_upcoming",
        "This premiere/live stream hasn't started.",
        False,
    ),
    (
        r"copyright|blocked it in your country|not available in your country|geo.?restrict",
        "Blocked (copyright or not available in the server's country).",
        False,
    ),
    (
        r"video unavailable|this video is( no longer)? (un)?available|has been removed",
        "Video unavailable (removed, private or region-locked).",
        False,
    ),
    (
        r"soundcloud.*(http error 404|not found)|http error 404.*soundcloud",
        "SoundCloud track not found (deleted, or a private track).",
        False,
    ),
    (r"soundcloud.*http error (401|403)", "Private or geo-blocked SoundCloud track.", False),
    (
        r"requested format is not available|no video formats|no formats found",
        "No downloadable audio format for this link.",
        False,
    ),
    (r"unsupported url|no suitable extractor", "Unsupported link.", False),
    (r"http error 429|too many requests", "Rate-limited by the site.", True),
    (
        r"timed? ?out|connection (reset|refused|aborted)|temporary failure in name resolution"
        r"|name or service not known|http error 5\d\d|incompleteread|remote end closed"
        r"|unable to download",
        "Network problem while downloading.",
        True,
    ),
]


def classify_error(raw: str) -> ImportFailed:
    text = raw.strip().splitlines()[-1] if raw.strip() else "unknown error"
    text = re.sub(r"^(ERROR:\s*)?(\[[^\]]+\]\s*)?([\w-]+:\s*)?", "", text) or text
    for pattern, friendly, retryable in _ERRORS:
        if re.search(pattern, raw, re.IGNORECASE):
            return ImportFailed(friendly, retryable=retryable)
    return ImportFailed(f"Download failed: {text[:300]}", retryable=True)


def track_meta(info: dict[str, Any]) -> TrackMeta:
    """Prefer the music metadata yt-dlp exposes (track/artist, e.g. YouTube Music
    'Topic' uploads); otherwise split 'Artist - Title' or fall back to the uploader."""
    track = info.get("track")
    artist = info.get("artist") or (info.get("artists") or [None])[0] or info.get("creator")
    album = info.get("album")
    if track and artist:
        return TrackMeta(clean_title(str(track)), clean_artist(str(artist)), album)
    raw_title = str(info.get("title") or track or "Unknown title")
    split_artist, rest = split_artist_title(raw_title)
    if split_artist:
        return TrackMeta(clean_title(rest), clean_artist(split_artist), album)
    uploader = info.get("uploader") or info.get("channel") or artist or "Unknown artist"
    return TrackMeta(clean_title(raw_title), clean_artist(str(uploader)), album)


def source_key(source: str, info: dict[str, Any]) -> str:
    vid = str(info.get("id") or "")
    return youtube_key(vid) if source == "youtube" else soundcloud_key(vid)


ProgressFn = Callable[[float], None]


class Downloader(Protocol):
    def extract(self, url: str) -> dict[str, Any]: ...

    def extract_playlist(self, url: str) -> PlaylistInfo: ...

    def download(self, info: dict[str, Any], dest_dir: Path, progress: ProgressFn) -> Path: ...


class YtDlpDownloader:
    def _opts(self) -> dict[str, Any]:
        s = get_settings()
        opts: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "noplaylist": True,
            # Only these extractors are loaded: no generic extractor = no arbitrary URLs.
            "allowed_extractors": ["youtube$", "soundcloud$"],
            "format": FORMAT,
            "socket_timeout": 30,
            "retries": 3,
            "fragment_retries": 3,
            "max_filesize": MAX_FILESIZE,
            "overwrites": True,
            "logger": _YtLogger(),
        }
        if s.ytdlp_cookies_file:
            # yt-dlp writes the cookie jar back on exit; the mounted file is read-only, so
            # give it a private writable copy.
            if not s.ytdlp_cookies_file.is_file():
                raise ImportFailed(
                    f"YTDLP_COOKIES_FILE is set but {s.ytdlp_cookies_file} doesn't exist.",
                    retryable=False,
                )
            copy = tmp_dir() / "ytdlp-cookies.txt"
            shutil.copyfile(s.ytdlp_cookies_file, copy)
            opts["cookiefile"] = str(copy)
        return opts

    def extract_playlist(self, url: str) -> PlaylistInfo:
        """List a playlist/set without downloading anything ("flat" extraction)."""
        opts = self._opts()
        opts.update(
            noplaylist=False,
            extract_flat="in_playlist",
            playlistend=MAX_PLAYLIST_TRACKS,
            allowed_extractors=["youtube$", "youtube:tab$", "soundcloud$", "soundcloud:set$"],
        )
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as e:
            failed = classify_error(str(e))
            raise ImportFailed(failed.message, retryable=failed.retryable) from None
        if not info or info.get("_type") != "playlist":
            raise ImportFailed("That link isn't a playlist.", retryable=False)
        return playlist_entries(info)

    def extract(self, url: str) -> dict[str, Any]:
        try:
            with yt_dlp.YoutubeDL(self._opts()) as ydl:
                info = ydl.extract_info(url, download=False)
        except yt_dlp.utils.DownloadError as e:
            raise classify_error(str(e)) from None
        if not info:
            raise ImportFailed("yt-dlp returned no information for this link.", retryable=True)
        if info.get("_type") in ("playlist", "multi_video"):
            raise ImportFailed("Playlists aren't supported yet.", retryable=False)
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming"):
            raise ImportFailed("Live streams can't be imported.", retryable=False)
        duration = info.get("duration") or 0
        if duration > MAX_DURATION_SECONDS:
            raise ImportFailed(
                f"Too long ({duration // 60} min, max {MAX_DURATION_SECONDS // 60} min).",
                retryable=False,
            )
        return info

    def download(self, info: dict[str, Any], dest_dir: Path, progress: ProgressFn) -> Path:
        def hook(d: dict[str, Any]) -> None:
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                if total:
                    progress(min(d.get("downloaded_bytes", 0) / total, 1.0))
                elif d.get("fragment_count"):
                    progress(min((d.get("fragment_index") or 0) / d["fragment_count"], 1.0))
            elif d.get("status") == "finished":
                progress(1.0)

        opts = self._opts()
        opts["outtmpl"] = {"default": str(dest_dir / "audio.%(ext)s")}
        opts["progress_hooks"] = [hook]
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                result = ydl.process_ie_result(info, download=True)
        except yt_dlp.utils.DownloadError as e:
            raise classify_error(str(e)) from None
        downloads = (result or {}).get("requested_downloads") or []
        path = Path(downloads[0]["filepath"]) if downloads else None
        if path is None or not path.is_file():
            raise ImportFailed("Download finished but no file was produced.", retryable=True)
        return path


class _YtLogger:
    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        log.debug("yt-dlp: %s", msg)

    def error(self, msg: str) -> None:
        log.debug("yt-dlp error: %s", msg)


class _ProgressReporter:
    """Thread-safe, throttled progress writes from yt-dlp's (worker-thread) hooks."""

    def __init__(self, loop: asyncio.AbstractEventLoop, job_id: uuid.UUID) -> None:
        self.loop = loop
        self.job_id = job_id
        self._last_value = -1
        self._last_time = 0.0

    async def write(self, value: int) -> None:
        async with get_sessionmaker()() as session:
            await jobs.set_progress(session, self.job_id, value)

    def __call__(self, fraction: float) -> None:
        # Downloading maps to 5..90 %; the rest is probing / conversion / saving.
        value = 5 + int(fraction * 85)
        now = time.monotonic()
        if value == self._last_value or (now - self._last_time < 1.0 and fraction < 1.0):
            return
        self._last_value, self._last_time = value, now
        asyncio.run_coroutine_threadsafe(self.write(value), self.loop)


async def process_import(
    job_id: uuid.UUID, payload: dict[str, Any], downloader: Downloader
) -> uuid.UUID:
    """Runs one import. Returns the new track id, raises ImportFailed otherwise."""
    sm = get_sessionmaker()
    url: str = payload["url"]
    source: str = payload["source"]

    if payload.get("needs_resolve"):
        try:
            target = await asyncio.to_thread(resolve_soundcloud_short, url)
        except UnsupportedUrl as e:
            raise ImportFailed(str(e), retryable=False) from None
        except OSError as e:
            raise ImportFailed(f"Could not resolve the short link ({e}).", retryable=True) from None
        if target.kind != "track":
            raise ImportFailed(
                "That short link points to a playlist: paste it again to import the whole set.",
                retryable=False,
            )
        url = target.url
        async with sm() as session:
            await jobs.merge_payload(session, job_id, url=url, needs_resolve=False)
    else:
        # Defence in depth: re-validate whatever is stored in the job.
        try:
            url = parse_import_url(url).url
        except UnsupportedUrl as e:
            raise ImportFailed(str(e), retryable=False) from None

    info = await asyncio.to_thread(downloader.extract, url)
    meta = track_meta(info)
    key = source_key(source, info)
    async with sm() as session:
        await jobs.merge_payload(session, job_id, title=meta.title, artist=meta.artist, key=key)
        await jobs.set_progress(session, job_id, 5)
        if dup := await find_duplicate(session, source_key=key):
            raise DuplicateTrackError(dup)

    work_dir = tmp_dir() / f"job-{job_id}"
    work_dir.mkdir(parents=True, exist_ok=True)
    try:
        reporter = _ProgressReporter(asyncio.get_running_loop(), job_id)
        path = await asyncio.to_thread(downloader.download, info, work_dir, reporter)
        # Let pending progress writes land before the final ones.
        await asyncio.sleep(0.05)
        async with sm() as session:
            await jobs.set_progress(session, job_id, 92)
        sha = await asyncio.to_thread(sha256_file, path)
        async with sm() as session:
            try:
                track = await ingest_file(
                    session,
                    path,
                    meta=meta,
                    source=source,
                    sha256=sha,
                    source_url=url,
                    source_key=key,
                )
            except NotAudioError as e:
                raise ImportFailed(
                    f"Downloaded file isn't usable audio: {e}", retryable=False
                ) from None
        return track.id
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


_UNAVAILABLE = ("[private video]", "[deleted video]", "[unavailable video]")


def playlist_entries(info: dict[str, Any]) -> PlaylistInfo:
    """Turn a flat playlist into validated single-track import targets."""
    entries: list[ImportUrl] = []
    skipped = 0
    for e in list(info.get("entries") or [])[:MAX_PLAYLIST_TRACKS]:
        if not e or str(e.get("title") or "").lower() in _UNAVAILABLE:
            skipped += 1
            continue
        try:
            target = parse_import_url(str(e.get("url") or ""))
        except UnsupportedUrl:
            skipped += 1
            continue
        if target.kind != "track" or target.needs_resolve:
            skipped += 1
            continue
        if target.source == "soundcloud" and e.get("id"):
            target = dataclasses.replace(target, key=soundcloud_key(e["id"]))
        entries.append(dataclasses.replace(target, playlist_url=None))
    title = str(info.get("title") or "Imported playlist").strip() or "Imported playlist"
    return PlaylistInfo(title=title, entries=entries, skipped=skipped)


@dataclasses.dataclass
class LinkInfo:
    kind: str  # "track" | "playlist"
    url: str
    source: str
    title: str | None = None
    count: int | None = None
    # for a video inside a playlist
    playlist_url: str | None = None
    playlist_title: str | None = None
    playlist_count: int | None = None


def inspect_link(raw: str, downloader: Downloader | None = None) -> LinkInfo:
    """What does this link point to? Blocking (network): call in a thread."""
    target = parse_import_url(raw)
    if target.needs_resolve:
        target = resolve_soundcloud_short(target.url)
    dl = downloader or YtDlpDownloader()
    if target.kind == "playlist":
        info = dl.extract_playlist(target.url)
        return LinkInfo("playlist", target.url, target.source, info.title, len(info.entries))
    out = LinkInfo("track", target.url, target.source)
    if target.playlist_url:
        try:
            info = dl.extract_playlist(target.playlist_url)
            out.playlist_url = target.playlist_url
            out.playlist_title, out.playlist_count = info.title, len(info.entries)
        except ImportFailed:
            pass  # e.g. a private list: just offer the video
    return out
