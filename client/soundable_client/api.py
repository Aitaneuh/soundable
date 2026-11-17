"""Async HTTP client for the soundable server's own API."""

from __future__ import annotations

import mimetypes
import secrets
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from soundable_client import __version__

TIMEOUT = httpx.Timeout(15.0, connect=10.0)
UPLOAD_TIMEOUT = httpx.Timeout(60.0, connect=10.0, write=300.0)


class ApiError(Exception):
    def __init__(self, message: str, status: int | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.status = status
        self.data = data


class AuthError(ApiError):
    """Token missing, invalid or revoked: the user must log in again."""


class NetworkError(ApiError):
    """Server unreachable / timeout."""


@dataclass(frozen=True)
class Track:
    id: str
    title: str
    artist: str
    album: str | None
    duration: float
    mime_type: str
    codec: str | None
    bitrate_kbps: int | None
    source: str
    source_url: str | None

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Track:
        return cls(
            id=d["id"],
            title=d["title"],
            artist=d["artist"],
            album=d.get("album"),
            duration=float(d.get("duration_seconds") or 0),
            mime_type=d.get("mime_type", ""),
            codec=d.get("codec"),
            bitrate_kbps=d.get("bitrate_kbps"),
            source=d.get("source", "upload"),
            source_url=d.get("source_url"),
        )


def _detail(resp: httpx.Response) -> tuple[str, Any]:
    try:
        data = resp.json()
    except ValueError:
        return resp.text.strip()[:200] or resp.reason_phrase, None
    detail = data.get("detail") if isinstance(data, dict) else None
    if isinstance(detail, list):  # FastAPI validation errors
        detail = "; ".join(str(e.get("msg", e)) for e in detail)
    return str(detail or resp.reason_phrase), data


class Api:
    def __init__(self, base_url: str, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=TIMEOUT,
            headers={"User-Agent": f"soundable-client/{__version__}"},
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def _request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        headers = {**self._headers(), **kw.pop("headers", {})}
        try:
            resp = await self._client.request(method, path, headers=headers, **kw)
        except httpx.TimeoutException as e:
            raise NetworkError("server timed out") from e
        except httpx.TransportError as e:
            raise NetworkError(f"cannot reach server ({e.__class__.__name__})") from e
        return self._check(resp)

    def _check(self, resp: httpx.Response) -> httpx.Response:
        if resp.is_success:
            return resp
        msg, data = _detail(resp)
        if resp.status_code == 401:
            raise AuthError(msg, 401, data)
        if resp.status_code in (502, 503, 504, 530):
            raise NetworkError(f"server unavailable ({resp.status_code})", resp.status_code)
        raise ApiError(msg, resp.status_code, data)

    # --- auth / health -----------------------------------------------------------------

    async def health(self) -> bool:
        try:
            await self._request("GET", "/api/health", timeout=5.0)
            return True
        except ApiError:
            return False

    async def login(self, password: str, name: str) -> str:
        resp = await self._request(
            "POST", "/api/auth/login", json={"password": password, "name": name}
        )
        self.token = resp.json()["token"]
        return self.token

    async def logout(self) -> None:
        await self._request("POST", "/api/auth/logout")
        self.token = None

    async def me(self) -> dict[str, Any]:
        return (await self._request("GET", "/api/auth/me")).json()

    # --- tracks --------------------------------------------------------------------------

    async def tracks(
        self, q: str | None = None, *, sort: str = "recent", limit: int = 50, offset: int = 0
    ) -> tuple[list[Track], int]:
        params: dict[str, Any] = {"sort": sort, "limit": limit, "offset": offset}
        if q:
            params["q"] = q
        data = (await self._request("GET", "/api/tracks", params=params)).json()
        return [Track.from_json(t) for t in data["items"]], data["total"]

    async def all_tracks(self, sort: str = "artist") -> list[Track]:
        out: list[Track] = []
        while True:
            page, total = await self.tracks(sort=sort, limit=500, offset=len(out))
            out += page
            if not page or len(out) >= total:
                return out

    async def track(self, track_id: str) -> Track:
        return Track.from_json((await self._request("GET", f"/api/tracks/{track_id}")).json())

    async def edit_track(self, track_id: str, **fields: str | None) -> Track:
        resp = await self._request("PATCH", f"/api/tracks/{track_id}", json=fields)
        return Track.from_json(resp.json())

    async def delete_track(self, track_id: str) -> None:
        await self._request("DELETE", f"/api/tracks/{track_id}")

    async def stream_url(self, track_id: str) -> str:
        """Absolute signed URL that mpv can open without headers."""
        data = (await self._request("GET", f"/api/tracks/{track_id}/stream-url")).json()
        return self.base_url + data["url"]

    async def upload(self, path: Path, progress: Callable[[int, int], None] | None = None) -> Track:
        """Streams the file as multipart/form-data, reporting (sent, total) bytes."""
        boundary = "soundable-" + secrets.token_hex(12)
        filename = path.name.replace('"', "'")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        head = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n"
        ).encode()
        tail = f"\r\n--{boundary}--\r\n".encode()
        size = path.stat().st_size
        total = len(head) + size + len(tail)

        async def body() -> AsyncIterator[bytes]:
            sent = 0
            yield head
            with path.open("rb") as f:
                while chunk := f.read(256 * 1024):
                    sent += len(chunk)
                    if progress:
                        progress(sent, size)
                    yield chunk
            yield tail

        resp = await self._request(
            "POST",
            "/api/tracks/upload",
            content=body(),
            headers={
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Content-Length": str(total),
            },
            timeout=UPLOAD_TIMEOUT,
        )
        return Track.from_json(resp.json())
