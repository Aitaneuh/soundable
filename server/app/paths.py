"""Forgiving URL paths for Subsonic clients.

Apps build request URLs as `<server url> + "/rest/<method>"`. If the user typed the
server URL with a trailing slash or with "/rest" included, that yields "//rest/ping.view"
or "/rest/rest/ping.view", which would 404. This ASGI middleware collapses repeated
slashes and normalises anything ending in ".../rest/<method>" to "/rest/<method>".
"""

from __future__ import annotations

import re
from typing import Any

_SLASHES = re.compile(r"/{2,}")
_REST = re.compile(r"^.*?/rest(?:/rest)*/([^/]+)/?$")


def normalize_path(path: str) -> str:
    path = _SLASHES.sub("/", path)
    m = _REST.match(path)
    if m and not path.startswith("/api/"):
        return f"/rest/{m.group(1)}"
    return path


class ForgivingPathMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            path = scope["path"]
            new = normalize_path(path)
            if new != path:
                scope = dict(scope)
                scope["path"] = new
                scope["raw_path"] = new.encode()
        await self.app(scope, receive, send)
