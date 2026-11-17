"""Mapping the flat track library onto Subsonic's artist / album / song model.

- Artists are the distinct `artist` values.
- Albums are (artist, album) pairs; tracks without an album go into a synthetic
  "Singles" album per artist.
- Artist and album ids encode their names (base64url), so they can be resolved without
  a lookup table. Renaming an artist/album changes its id; clients simply re-sync.
- No coverArt attributes are ever sent, so clients don't request artwork.
"""

from __future__ import annotations

import base64
import binascii
import unicodedata
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from app.models import Playlist, Track
from app.subsonic.response import SubsonicError, iso

SINGLES = "Singles"
IGNORED_ARTICLES = "The El La Los Las Le Les"
_ARTICLES = tuple(a + " " for a in IGNORED_ARTICLES.split())
_SEP = "\x1f"


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode().rstrip("=")


def _unb64(s: str) -> str:
    try:
        return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise SubsonicError(70, "Not found") from None


def artist_id(name: str) -> str:
    return "ar-" + _b64(name)


def album_id(artist: str, album: str | None) -> str:
    return "al-" + _b64(artist + _SEP + (album or ""))


def parse_artist_id(aid: str) -> str:
    if not aid.startswith("ar-"):
        raise SubsonicError(70, "Artist not found")
    return _unb64(aid[3:])


def parse_album_id(aid: str) -> tuple[str, str | None]:
    if not aid.startswith("al-"):
        raise SubsonicError(70, "Album not found")
    artist, sep, album = _unb64(aid[3:]).partition(_SEP)
    if not sep:
        raise SubsonicError(70, "Album not found")
    return artist, album or None


def index_letter(name: str) -> str:
    n = name.strip()
    for art in _ARTICLES:
        if n.lower().startswith(art.lower()) and len(n) > len(art):
            n = n[len(art) :]
            break
    folded = unicodedata.normalize("NFKD", n[:1]).encode("ascii", "ignore").decode()
    return folded.upper() if folded.isalpha() else "#"


def sort_name(name: str) -> str:
    n = name.strip()
    for art in _ARTICLES:
        if n.lower().startswith(art.lower()) and len(n) > len(art):
            return n[len(art) :].casefold()
    return n.casefold()


def _suffix(t: Track) -> str:
    return PurePosixPath(t.file_path).suffix.lstrip(".") or "mp3"


def _safe(part: str) -> str:
    return part.replace("/", "_").replace("\\", "_").strip() or "_"


def song(t: Track) -> dict[str, Any]:
    album_name = t.album or SINGLES
    suffix = _suffix(t)
    return {
        "id": str(t.id),
        "parent": album_id(t.artist, t.album),
        "isDir": False,
        "title": t.title,
        "album": album_name,
        "artist": t.artist,
        "size": t.file_size,
        "contentType": t.mime_type,
        "suffix": suffix,
        "duration": round(t.duration_seconds),
        "bitRate": t.bitrate_kbps,
        "path": f"{_safe(t.artist)}/{_safe(album_name)}/{_safe(t.title)}.{suffix}",
        "isVideo": False,
        "created": iso(t.created_at),
        "albumId": album_id(t.artist, t.album),
        "artistId": artist_id(t.artist),
        "type": "music",
        "mediaType": "song",
        "starred": iso(t.starred_at),
    }


@dataclass
class Album:
    artist: str
    name: str | None  # None = the synthetic "Singles" album
    tracks: list[Track] = field(default_factory=list)

    @property
    def id(self) -> str:
        return album_id(self.artist, self.name)

    @property
    def created(self) -> datetime:
        return min(t.created_at for t in self.tracks)

    @property
    def display_name(self) -> str:
        return self.name or SINGLES

    def sorted_tracks(self) -> list[Track]:
        return sorted(self.tracks, key=lambda t: (t.created_at, t.title.casefold()))

    def id3(self) -> dict[str, Any]:
        starred = [t.starred_at for t in self.tracks if t.starred_at]
        return {
            "id": self.id,
            "name": self.display_name,
            "artist": self.artist,
            "artistId": artist_id(self.artist),
            "songCount": len(self.tracks),
            "duration": round(sum(t.duration_seconds for t in self.tracks)),
            "created": iso(self.created),
            "starred": iso(max(starred)) if starred and len(starred) == len(self.tracks) else None,
        }

    def as_dir(self) -> dict[str, Any]:
        """Directory-style child (getMusicDirectory / getAlbumList / search2)."""
        return {
            "id": self.id,
            "parent": artist_id(self.artist),
            "isDir": True,
            "title": self.display_name,
            "album": self.display_name,
            "artist": self.artist,
            "created": iso(self.created),
        }


@dataclass
class Library:
    """In-memory view of the library for one request. A personal library is a few
    thousand rows at most, so aggregating in Python is simple and fast enough."""

    tracks: list[Track]
    artists: dict[str, list[Album]] = field(default_factory=dict)
    albums: dict[str, Album] = field(default_factory=dict)

    def __post_init__(self) -> None:
        by_album: dict[tuple[str, str | None], Album] = {}
        for t in self.tracks:
            key = (t.artist, t.album)
            if key not in by_album:
                by_album[key] = Album(t.artist, t.album)
            by_album[key].tracks.append(t)
        artists: dict[str, list[Album]] = defaultdict(list)
        for alb in by_album.values():
            artists[alb.artist].append(alb)
            self.albums[alb.id] = alb
        self.artists = {
            name: sorted(albs, key=lambda a: (a.name is None, (a.name or "").casefold()))
            for name, albs in sorted(artists.items(), key=lambda kv: sort_name(kv[0]))
        }

    def artist_entry(self, name: str) -> dict[str, Any]:
        return {"id": artist_id(name), "name": name, "albumCount": len(self.artists[name])}

    def indexed_artists(self) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for name in self.artists:
            groups[index_letter(name)].append(self.artist_entry(name))
        letters = sorted(groups, key=lambda x: (x == "#", x))
        return [{"name": letter, "artist": groups[letter]} for letter in letters]

    def album_by_id(self, aid: str) -> Album:
        artist, name = parse_album_id(aid)
        alb = self.albums.get(album_id(artist, name))
        if alb is None:
            raise SubsonicError(70, "Album not found")
        return alb

    def artist_albums(self, aid: str) -> tuple[str, list[Album]]:
        name = parse_artist_id(aid)
        if name not in self.artists:
            raise SubsonicError(70, "Artist not found")
        return name, self.artists[name]


def playlist_entry(
    p: Playlist,
    owner: str,
    tracks: list[Track] | None = None,
    *,
    count: int | None = None,
    duration: float | None = None,
) -> dict[str, Any]:
    if tracks is not None:
        count = len(tracks)
        duration = sum(t.duration_seconds for t in tracks)
    return {
        "id": str(p.id),
        "name": p.name,
        "comment": p.comment,
        "owner": owner,
        "public": False,
        "songCount": count or 0,
        "duration": round(duration or 0),
        "created": iso(p.created_at),
        "changed": iso(p.updated_at),
    }
