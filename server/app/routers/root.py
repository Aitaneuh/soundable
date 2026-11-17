"""The bare server URL. Subsonic apps (Amperfy) probe it before logging in and treat a
404 as "no server here", so it must answer 200."""

from __future__ import annotations

from fastapi import APIRouter, Response
from fastapi.responses import HTMLResponse

router = APIRouter(include_in_schema=False)

PAGE = """<!doctype html><html><head><meta charset="utf-8"><title>soundable</title>
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem">
<h1>soundable</h1>
<p>This is a soundable music server. Use the <code>soundable</code> terminal client, or a
Subsonic app (e.g. Amperfy) with this address as the server URL.</p>
</body></html>"""


@router.api_route("/", methods=["GET", "HEAD"])
async def root() -> HTMLResponse:
    return HTMLResponse(PAGE)


@router.api_route("/favicon.ico", methods=["GET", "HEAD"])
async def favicon() -> Response:
    return Response(status_code=204)
