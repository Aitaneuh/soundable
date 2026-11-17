"""Subsonic response envelope, rendered as XML (default) or JSON (f=json / f=jsonp).

Payloads are plain dicts with one convention that maps onto both formats:
- scalar value  -> XML attribute / JSON field
- dict          -> child element / JSON object
- list          -> repeated child elements / JSON array (of objects or scalars)
- key "value"   -> XML text content / JSON "value" field
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any
from xml.etree import ElementTree as ET

from fastapi.responses import Response

API_VERSION = "1.16.1"
SERVER_TYPE = "soundable"
SERVER_VERSION = "0.1.0"
NAMESPACE = "http://subsonic.org/restapi"


class SubsonicError(Exception):
    """Subsonic error codes: 0 generic, 10 missing parameter, 20/30 version mismatch,
    40 wrong credentials, 41 token auth unsupported, 42 auth mechanism unsupported,
    50 not authorized, 70 not found."""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _xml_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, datetime):
        return iso(v) or ""
    return str(v)


def _to_xml(parent: ET.Element, name: str, value: Any) -> None:
    if isinstance(value, list):
        for item in value:
            _to_xml(parent, name, item)
        return
    el = ET.SubElement(parent, name)
    if isinstance(value, dict):
        _fill(el, value)
    elif value is not None:
        el.text = _xml_scalar(value)


def _fill(el: ET.Element, data: dict[str, Any]) -> None:
    for k, v in data.items():
        if v is None:
            continue
        if k == "value" and not isinstance(v, dict | list):
            el.text = _xml_scalar(v)
        elif isinstance(v, dict | list):
            _to_xml(el, k, v)
        else:
            el.set(k, _xml_scalar(v))


def _to_json(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _to_json(x) for k, x in v.items() if x is not None}
    if isinstance(v, list):
        return [_to_json(x) for x in v]
    if isinstance(v, datetime):
        return iso(v)
    return v


def envelope(
    payload: dict[str, Any] | None, *, error: SubsonicError | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "status": "failed" if error else "ok",
        "version": API_VERSION,
        "type": SERVER_TYPE,
        "serverVersion": SERVER_VERSION,
        "openSubsonic": True,
    }
    if error:
        body["error"] = {"code": error.code, "message": error.message}
    elif payload:
        body.update(payload)
    return body


_JSONP_CALLBACK = re.compile(r"^[A-Za-z_$][\w$.]{0,100}$")


def render(
    payload: dict[str, Any] | None,
    fmt: str | None,
    *,
    error: SubsonicError | None = None,
    callback: str | None = None,
) -> Response:
    body = envelope(payload, error=error)
    fmt = (fmt or "xml").lower()
    if fmt in ("json", "jsonp"):
        text = json.dumps({"subsonic-response": _to_json(body)}, ensure_ascii=False)
        if fmt == "jsonp" and callback and _JSONP_CALLBACK.match(callback):
            return Response(f"{callback}({text});", media_type="application/javascript")
        return Response(text, media_type="application/json")
    root = ET.Element("subsonic-response", {"xmlns": NAMESPACE})
    _fill(root, body)
    xml = ET.tostring(root, encoding="unicode")
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?>\n' + xml,
        media_type="text/xml; charset=utf-8",
    )
