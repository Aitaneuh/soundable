"""Subsonic / OpenSubsonic API: /rest/<method> and /rest/<method>.view, GET or POST.

A subset aimed at Amperfy (main target), play:Sub and substreamer. Unsupported features
(podcasts, radio, genres, lyrics, similar songs...) answer with valid empty results so
clients don't treat them as failures.
"""

from __future__ import annotations

import logging
import random
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import playlists as pl_svc
from app.config import get_settings
from app.db import get_sessionmaker
from app.deps import client_ip
from app.library import track_path
from app.models import Track
from app.routers.tracks import file_response, search_filter
from app.subsonic.auth import check_credentials
from app.subsonic.mapping import (
    IGNORED_ARTICLES,
    Library,
    artist_id,
    playlist_entry,
    song,
    sort_name,
)
from app.subsonic.response import SubsonicError, render
from app.transcode import mp3_stream, pick_bitrate, should_transcode

log = logging.getLogger(__name__)
router = APIRouter(tags=["subsonic"])


@dataclass
class Ctx:
    request: Request
    session: AsyncSession
    params: dict[str, list[str]]

    def get(self, name: str, default: str | None = None) -> str | None:
        v = self.params.get(name)
        return v[0] if v else default

    def all(self, name: str) -> list[str]:
        return self.params.get(name, [])

    def require(self, name: str) -> str:
        v = self.get(name)
        if v is None or v == "":
            raise SubsonicError(10, f"Required parameter is missing: {name}")
        return v

    def int(self, name: str, default: int, lo: int = 0, hi: int = 10_000) -> int:
        raw = self.get(name)
        if raw in (None, ""):
            return default
        try:
            return max(lo, min(hi, int(float(raw))))  # type: ignore[arg-type]
        except ValueError:
            raise SubsonicError(0, f"Invalid value for {name}: {raw!r}") from None

    @property
    def owner(self) -> str:
        return get_settings().subsonic_user


Handler = Callable[[Ctx], Awaitable[dict[str, Any] | Response | None]]
HANDLERS: dict[str, Handler] = {}


def endpoint(*names: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        for n in names:
            HANDLERS[n.lower()] = fn
        return fn

    return deco


async def _all_tracks(session: AsyncSession) -> list[Track]:
    return list(await session.scalars(select(Track).order_by(Track.created_at, Track.id)))


async def _library(ctx: Ctx) -> Library:
    return Library(await _all_tracks(ctx.session))


async def _track(ctx: Ctx, raw_id: str) -> Track:
    try:
        tid = uuid.UUID(raw_id)
    except ValueError:
        raise SubsonicError(70, "Song not found") from None
    t = await ctx.session.get(Track, tid)
    if t is None:
        raise SubsonicError(70, "Song not found")
    return t


def _uuid(raw: str, what: str) -> uuid.UUID:
    try:
        return uuid.UUID(raw)
    except ValueError:
        raise SubsonicError(70, f"{what} not found") from None


# --- system ---------------------------------------------------------------------------


@endpoint("ping")
async def ping(ctx: Ctx) -> None:
    return None


@endpoint("getLicense")
async def get_license(ctx: Ctx) -> dict[str, Any]:
    return {"license": {"valid": True, "email": "", "licenseExpires": "2099-12-31T00:00:00.000Z"}}


@endpoint("getOpenSubsonicExtensions")
async def extensions(ctx: Ctx) -> dict[str, Any]:
    return {
        "openSubsonicExtensions": [
            {"name": "transcodeOffset", "versions": [1]},
            {"name": "formPost", "versions": [1]},
        ]
    }


@endpoint("getMusicFolders")
async def music_folders(ctx: Ctx) -> dict[str, Any]:
    return {"musicFolders": {"musicFolder": [{"id": 1, "name": "Music"}]}}


@endpoint("getUser")
async def get_user(ctx: Ctx) -> dict[str, Any]:
    enabled = (
        "adminRole",
        "settingsRole",
        "downloadRole",
        "playlistRole",
        "commentRole",
        "streamRole",
        "scrobblingEnabled",
    )
    disabled = (
        "uploadRole",
        "coverArtRole",
        "podcastRole",
        "jukeboxRole",
        "shareRole",
        "videoConversionRole",
    )
    user: dict[str, Any] = {"username": ctx.owner, "email": ""}
    user.update({r: True for r in enabled})
    user.update({r: False for r in disabled})
    user["folder"] = [1]
    return {"user": user}


@endpoint("getScanStatus", "startScan")
async def scan_status(ctx: Ctx) -> dict[str, Any]:
    count = await ctx.session.scalar(select(func.count()).select_from(Track))
    return {"scanStatus": {"scanning": False, "count": count or 0}}


# --- browsing ---------------------------------------------------------------------------


@endpoint("getArtists")
async def get_artists(ctx: Ctx) -> dict[str, Any]:
    lib = await _library(ctx)
    return {"artists": {"ignoredArticles": IGNORED_ARTICLES, "index": lib.indexed_artists()}}


@endpoint("getIndexes")
async def get_indexes(ctx: Ctx) -> dict[str, Any]:
    lib = await _library(ctx)
    last = max((t.updated_at for t in lib.tracks), default=datetime.now(UTC))
    index = [
        {"name": g["name"], "artist": [{"id": a["id"], "name": a["name"]} for a in g["artist"]]}
        for g in lib.indexed_artists()
    ]
    return {
        "indexes": {
            "lastModified": int(last.timestamp() * 1000),
            "ignoredArticles": IGNORED_ARTICLES,
            "index": index,
        }
    }


@endpoint("getArtist")
async def get_artist(ctx: Ctx) -> dict[str, Any]:
    lib = await _library(ctx)
    name, albums = lib.artist_albums(ctx.require("id"))
    return {"artist": {**lib.artist_entry(name), "album": [a.id3() for a in albums]}}


@endpoint("getAlbum")
async def get_album(ctx: Ctx) -> dict[str, Any]:
    lib = await _library(ctx)
    alb = lib.album_by_id(ctx.require("id"))
    return {"album": {**alb.id3(), "song": [song(t) for t in alb.sorted_tracks()]}}


@endpoint("getMusicDirectory")
async def get_music_directory(ctx: Ctx) -> dict[str, Any]:
    did = ctx.require("id")
    lib = await _library(ctx)
    if did.startswith("ar-"):
        name, albums = lib.artist_albums(did)
        return {"directory": {"id": did, "name": name, "child": [a.as_dir() for a in albums]}}
    if did.startswith("al-"):
        alb = lib.album_by_id(did)
        return {
            "directory": {
                "id": did,
                "parent": artist_id(alb.artist),
                "name": alb.display_name,
                "child": [song(t) for t in alb.sorted_tracks()],
            }
        }
    if did == "1":  # the music folder itself
        children = [
            {"id": artist_id(n), "parent": "1", "isDir": True, "title": n} for n in lib.artists
        ]
        return {"directory": {"id": "1", "name": "Music", "child": children}}
    raise SubsonicError(70, "Directory not found")


def _album_list(lib: Library, ctx: Ctx) -> list[Any]:
    kind = ctx.require("type")
    size = ctx.int("size", 10, 1, 500)
    offset = ctx.int("offset", 0, 0, 1_000_000)
    albums = list(lib.albums.values())
    if kind == "random":
        random.shuffle(albums)
    elif kind in ("newest", "recent", "frequent", "highest"):
        # No play statistics: "recent/frequent/highest" fall back to newest.
        albums.sort(key=lambda a: a.created, reverse=True)
    elif kind == "alphabeticalByName":
        albums.sort(key=lambda a: (sort_name(a.display_name), sort_name(a.artist)))
    elif kind == "alphabeticalByArtist":
        albums.sort(key=lambda a: (sort_name(a.artist), sort_name(a.display_name)))
    elif kind == "starred":
        albums = [a for a in albums if all(t.starred_at for t in a.tracks)]
        albums.sort(key=lambda a: sort_name(a.display_name))
    elif kind in ("byYear", "byGenre"):
        albums = []  # no year/genre metadata
    else:
        raise SubsonicError(0, f"Unknown album list type: {kind}")
    return albums[offset : offset + size]


@endpoint("getAlbumList2")
async def get_album_list2(ctx: Ctx) -> dict[str, Any]:
    lib = await _library(ctx)
    return {"albumList2": {"album": [a.id3() for a in _album_list(lib, ctx)]}}


@endpoint("getAlbumList")
async def get_album_list(ctx: Ctx) -> dict[str, Any]:
    lib = await _library(ctx)
    return {"albumList": {"album": [a.as_dir() for a in _album_list(lib, ctx)]}}


@endpoint("getSong")
async def get_song(ctx: Ctx) -> dict[str, Any]:
    return {"song": song(await _track(ctx, ctx.require("id")))}


@endpoint("getRandomSongs")
async def get_random_songs(ctx: Ctx) -> dict[str, Any]:
    size = ctx.int("size", 10, 1, 500)
    rows = await ctx.session.scalars(select(Track).order_by(func.random()).limit(size))
    return {"randomSongs": {"song": [song(t) for t in rows]}}


@endpoint("getGenres")
async def get_genres(ctx: Ctx) -> dict[str, Any]:
    return {"genres": {"genre": []}}


@endpoint("getSongsByGenre")
async def songs_by_genre(ctx: Ctx) -> dict[str, Any]:
    return {"songsByGenre": {"song": []}}


@endpoint("getTopSongs")
async def top_songs(ctx: Ctx) -> dict[str, Any]:
    return {"topSongs": {"song": []}}


@endpoint("getSimilarSongs", "getSimilarSongs2")
async def similar_songs(ctx: Ctx) -> dict[str, Any]:
    key = (
        "similarSongs2"
        if ctx.request.path_params["method"].lower().startswith("getsimilarsongs2")
        else "similarSongs"
    )
    return {key: {"song": []}}


@endpoint("getArtistInfo", "getArtistInfo2")
async def artist_info(ctx: Ctx) -> dict[str, Any]:
    key = "artistInfo2" if "2" in ctx.request.path_params["method"] else "artistInfo"
    return {key: {}}


@endpoint("getAlbumInfo", "getAlbumInfo2")
async def album_info(ctx: Ctx) -> dict[str, Any]:
    return {"albumInfo": {}}


@endpoint("getLyrics")
async def lyrics(ctx: Ctx) -> dict[str, Any]:
    return {"lyrics": {}}


@endpoint("getNowPlaying")
async def now_playing(ctx: Ctx) -> dict[str, Any]:
    return {"nowPlaying": {"entry": []}}


@endpoint("getPodcasts")
async def podcasts(ctx: Ctx) -> dict[str, Any]:
    return {"podcasts": {"channel": []}}


@endpoint("getNewestPodcasts")
async def newest_podcasts(ctx: Ctx) -> dict[str, Any]:
    return {"newestPodcasts": {"episode": []}}


@endpoint("getInternetRadioStations")
async def radio(ctx: Ctx) -> dict[str, Any]:
    return {"internetRadioStations": {"internetRadioStation": []}}


@endpoint("getBookmarks")
async def bookmarks(ctx: Ctx) -> dict[str, Any]:
    return {"bookmarks": {"bookmark": []}}


@endpoint("getPlayQueue")
async def get_play_queue(ctx: Ctx) -> None:
    return None  # no saved queue


@endpoint("savePlayQueue", "scrobble", "setRating", "createBookmark", "deleteBookmark")
async def accepted(ctx: Ctx) -> None:
    return None  # accepted and ignored (no play statistics / ratings)


# --- search -----------------------------------------------------------------------------


def _match_all(q: str | None) -> bool:
    return q is None or q.strip() in ("", '""', "''", "*")


def _contains(hay: str, words: list[str]) -> bool:
    folded = _fold(hay)
    return all(w in folded for w in words)


def _fold(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKD", s.casefold()) if not unicodedata.combining(c)
    )


async def _search(ctx: Ctx) -> tuple[list[str], list[Any], list[Track]]:
    q = ctx.get("query")
    artist_count = ctx.int("artistCount", 20, 0, 500)
    artist_offset = ctx.int("artistOffset", 0, 0, 1_000_000)
    album_count = ctx.int("albumCount", 20, 0, 500)
    album_offset = ctx.int("albumOffset", 0, 0, 1_000_000)
    song_count = ctx.int("songCount", 20, 0, 500)
    song_offset = ctx.int("songOffset", 0, 0, 1_000_000)

    everything = _match_all(q)
    words = [] if everything else _fold(q.strip().strip('"')).split()  # type: ignore[union-attr]

    artists: list[str] = []
    albums: list[Any] = []
    if artist_count or album_count:
        lib = await _library(ctx)
        if artist_count:
            names = [n for n in lib.artists if everything or _contains(n, words)]
            artists = names[artist_offset : artist_offset + artist_count]
        if album_count:
            albs = sorted(
                lib.albums.values(), key=lambda a: (sort_name(a.artist), sort_name(a.display_name))
            )
            albs = [
                a for a in albs if everything or _contains(f"{a.display_name} {a.artist}", words)
            ]
            albums = albs[album_offset : album_offset + album_count]

    songs: list[Track] = []
    if song_count:
        # Stable order (created_at, id) so clients can page through the whole library.
        stmt = select(Track).order_by(Track.created_at, Track.id)
        if not everything:
            stmt = search_filter(stmt, " ".join(words))
        songs = list(await ctx.session.scalars(stmt.offset(song_offset).limit(song_count)))
    return artists, albums, songs


@endpoint("search3")
async def search3(ctx: Ctx) -> dict[str, Any]:
    artists, albums, songs = await _search(ctx)
    lib_counts = Library([]) if not artists else await _library(ctx)
    return {
        "searchResult3": {
            "artist": [lib_counts.artist_entry(n) for n in artists],
            "album": [a.id3() for a in albums],
            "song": [song(t) for t in songs],
        }
    }


@endpoint("search2")
async def search2(ctx: Ctx) -> dict[str, Any]:
    artists, albums, songs = await _search(ctx)
    return {
        "searchResult2": {
            "artist": [{"id": artist_id(n), "name": n} for n in artists],
            "album": [a.as_dir() for a in albums],
            "song": [song(t) for t in songs],
        }
    }


# --- starring ---------------------------------------------------------------------------


async def _set_star(ctx: Ctx, on: bool) -> None:
    now = datetime.now(UTC) if on else None
    for raw in ctx.all("id"):
        try:
            tid = uuid.UUID(raw)
        except ValueError:
            continue  # album/artist stars: accepted, not stored
        t = await ctx.session.get(Track, tid)
        if t is not None:
            t.starred_at = now
    await ctx.session.commit()


@endpoint("star")
async def star(ctx: Ctx) -> None:
    await _set_star(ctx, True)


@endpoint("unstar")
async def unstar(ctx: Ctx) -> None:
    await _set_star(ctx, False)


async def _starred(ctx: Ctx) -> list[Track]:
    rows = await ctx.session.scalars(
        select(Track).where(Track.starred_at.is_not(None)).order_by(Track.starred_at.desc())
    )
    return list(rows)


@endpoint("getStarred2")
async def get_starred2(ctx: Ctx) -> dict[str, Any]:
    return {"starred2": {"artist": [], "album": [], "song": [song(t) for t in await _starred(ctx)]}}


@endpoint("getStarred")
async def get_starred(ctx: Ctx) -> dict[str, Any]:
    return {"starred": {"artist": [], "album": [], "song": [song(t) for t in await _starred(ctx)]}}


# --- playlists --------------------------------------------------------------------------


@endpoint("getPlaylists")
async def get_playlists(ctx: Ctx) -> dict[str, Any]:
    summaries = await pl_svc.list_playlists(ctx.session)
    return {
        "playlists": {
            "playlist": [
                playlist_entry(
                    s.playlist, ctx.owner, count=s.track_count, duration=s.duration_seconds
                )
                for s in summaries
            ]
        }
    }


async def _playlist_payload(ctx: Ctx, pid: uuid.UUID) -> dict[str, Any]:
    try:
        p = await pl_svc.get_playlist(ctx.session, pid)
    except pl_svc.PlaylistNotFound:
        raise SubsonicError(70, "Playlist not found") from None
    tracks = [e.track for e in p.entries]
    return {
        "playlist": {**playlist_entry(p, ctx.owner, tracks), "entry": [song(t) for t in tracks]}
    }


@endpoint("getPlaylist")
async def get_playlist(ctx: Ctx) -> dict[str, Any]:
    return await _playlist_payload(ctx, _uuid(ctx.require("id"), "Playlist"))


def _song_ids(ctx: Ctx, name: str) -> list[uuid.UUID]:
    return [_uuid(s, "Song") for s in ctx.all(name)]


async def _unique_name(ctx: Ctx, name: str) -> str:
    existing = {s.playlist.name.casefold() for s in await pl_svc.list_playlists(ctx.session)}
    candidate, n = name, 2
    while candidate.casefold() in existing:
        candidate, n = f"{name} ({n})", n + 1
    return candidate


@endpoint("createPlaylist")
async def create_playlist(ctx: Ctx) -> dict[str, Any]:
    ids = _song_ids(ctx, "songId")
    try:
        if pid_raw := ctx.get("playlistId"):  # "create" on an existing id = replace its songs
            pid = _uuid(pid_raw, "Playlist")
            await pl_svc.set_tracks(ctx.session, pid, ids)
            if name := ctx.get("name"):
                await pl_svc.update_playlist(ctx.session, pid, name=name)
        else:
            # Subsonic allows duplicate names; we don't, so suffix " (2)" instead of failing.
            name = await _unique_name(ctx, ctx.require("name"))
            pid = (await pl_svc.create_playlist(ctx.session, name, ids)).id
    except pl_svc.PlaylistNotFound:
        raise SubsonicError(70, "Playlist not found") from None
    except pl_svc.PlaylistError as e:
        raise SubsonicError(0, str(e)) from None
    return await _playlist_payload(ctx, pid)


@endpoint("updatePlaylist")
async def update_playlist(ctx: Ctx) -> None:
    pid = _uuid(ctx.require("playlistId"), "Playlist")
    try:
        if ctx.get("name") is not None or ctx.get("comment") is not None:
            await pl_svc.update_playlist(
                ctx.session,
                pid,
                name=ctx.get("name") or None,
                comment=ctx.get("comment"),
                set_comment=ctx.get("comment") is not None,
            )
        remove = [int(i) for i in ctx.all("songIndexToRemove") if i.lstrip("-").isdigit()]
        if remove:
            await pl_svc.remove_positions(ctx.session, pid, remove)
        if add := _song_ids(ctx, "songIdToAdd"):
            await pl_svc.add_tracks(ctx.session, pid, add)
    except pl_svc.PlaylistNotFound:
        raise SubsonicError(70, "Playlist not found") from None
    except pl_svc.PlaylistError as e:
        raise SubsonicError(0, str(e)) from None


@endpoint("deletePlaylist")
async def delete_playlist(ctx: Ctx) -> None:
    try:
        await pl_svc.delete_playlist(ctx.session, _uuid(ctx.require("id"), "Playlist"))
    except pl_svc.PlaylistNotFound:
        raise SubsonicError(70, "Playlist not found") from None


# --- media ------------------------------------------------------------------------------


@endpoint("stream")
async def stream(ctx: Ctx) -> Response:
    t = await _track(ctx, ctx.require("id"))
    max_kbps = ctx.int("maxBitRate", 0, 0, 10_000)
    fmt = ctx.get("format")
    suffix = t.file_path.rsplit(".", 1)[-1].lower()
    if should_transcode(
        fmt=fmt, max_kbps=max_kbps, source_kbps=t.bitrate_kbps, source_suffix=suffix
    ):
        offset = float(ctx.int("timeOffset", 0, 0, 24 * 3600))
        return await mp3_stream(track_path(t), pick_bitrate(max_kbps), offset)
    return file_response(t)


@endpoint("download")
async def download(ctx: Ctx) -> Response:
    t = await _track(ctx, ctx.require("id"))
    suffix = t.file_path.rsplit(".", 1)[-1]
    name = f"{t.artist} - {t.title}.{suffix}".replace("/", "_").replace("\\", "_")
    return file_response(t, download_name=name)


@endpoint("getCoverArt")
async def cover_art(ctx: Ctx) -> None:
    # soundable stores no artwork and never advertises coverArt ids.
    raise SubsonicError(70, "Cover art not available")


# --- dispatch ---------------------------------------------------------------------------


async def _params(request: Request) -> dict[str, list[str]]:
    params: dict[str, list[str]] = {}
    for k, v in request.query_params.multi_items():
        params.setdefault(k, []).append(v)
    if request.method == "POST":
        ctype = request.headers.get("content-type", "")
        if ctype.startswith("application/x-www-form-urlencoded"):
            form = await request.form()
            for k, v in form.multi_items():
                if isinstance(v, str):
                    params.setdefault(k, []).append(v)
    return params


@router.api_route("/rest/{method}", methods=["GET", "POST"], include_in_schema=False)
async def dispatch(method: str, request: Request) -> Response:
    name = method.removesuffix(".view").lower()
    raw = await _params(request)
    flat = {k: v[0] for k, v in raw.items() if v}
    fmt, callback = flat.get("f"), flat.get("callback")
    try:
        check_credentials(flat, client_ip(request))
        handler = HANDLERS.get(name)
        if handler is None:
            raise SubsonicError(0, f"Not implemented: {method}")
        async with get_sessionmaker()() as session:
            result = await handler(Ctx(request, session, raw))
    except SubsonicError as e:
        if e.code not in (40, 70):
            log.info("subsonic %s failed: %s (%s)", method, e.message, e.code)
        return render(None, fmt, error=e, callback=callback)
    if isinstance(result, Response):
        return result
    return render(result, fmt, callback=callback)
