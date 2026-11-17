"""Streaming multipart parser for audio uploads.

Starlette's `request.form()` spools the whole file to a temp file before our code runs,
so a size limit could only be checked after the fact. Parsing the stream ourselves lets
us write straight into MUSIC_DIR/.tmp, hash on the fly, and abort as soon as the limit
is exceeded.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO

from fastapi import Request
from python_multipart.multipart import MultipartParser, parse_options_header

from app.library import new_tmp_path

TEXT_FIELDS = frozenset({"title", "artist", "album"})
MAX_TEXT_FIELD = 1000


class UploadTooLarge(Exception):
    pass


class BadUpload(Exception):
    pass


@dataclass
class ParsedUpload:
    path: Path
    filename: str
    size: int
    sha256: str
    fields: dict[str, str] = field(default_factory=dict)


def safe_filename(raw: str) -> str:
    """Keep only the last path component (browsers/curl on Windows may send full paths).
    Only used for display / metadata guessing, never as a path on disk."""
    return raw.replace("\\", "/").rsplit("/", 1)[-1].strip()[:255]


class _Sink:
    def __init__(self, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        self._header_field = bytearray()
        self._header_value = bytearray()
        self._headers: dict[bytes, bytes] = {}
        self._target: str | None = None  # "file", a text field name, or None (ignored)
        self._text = bytearray()
        self.file: BinaryIO | None = None
        self.path: Path | None = None
        self.filename = ""
        self.size = 0
        self.sha = hashlib.sha256()
        self.fields: dict[str, str] = {}

    # header callbacks
    def on_part_begin(self) -> None:
        self._headers = {}
        self._target = None

    def on_header_field(self, data: bytes, start: int, end: int) -> None:
        self._header_field += data[start:end]

    def on_header_value(self, data: bytes, start: int, end: int) -> None:
        self._header_value += data[start:end]

    def on_header_end(self) -> None:
        self._headers[bytes(self._header_field).lower()] = bytes(self._header_value)
        self._header_field.clear()
        self._header_value.clear()

    def on_headers_finished(self) -> None:
        _, opts = parse_options_header(self._headers.get(b"content-disposition", b""))
        name = opts.get(b"name", b"").decode(errors="replace")
        filename = opts.get(b"filename")
        if name == "file" and filename is not None:
            if self.file is not None:
                raise BadUpload("only one file per request")
            self.filename = safe_filename(filename.decode(errors="replace"))
            self.path = new_tmp_path(Path(self.filename).suffix.lower()[:10])
            self.file = self.path.open("wb")
            self._target = "file"
        elif name in TEXT_FIELDS:
            self._target = name
            self._text.clear()

    # body callbacks
    def on_part_data(self, data: bytes, start: int, end: int) -> None:
        chunk = data[start:end]
        if self._target == "file":
            assert self.file is not None
            self.size += len(chunk)
            if self.size > self.max_bytes:
                raise UploadTooLarge
            self.file.write(chunk)
            self.sha.update(chunk)
        elif self._target is not None:
            if len(self._text) + len(chunk) > MAX_TEXT_FIELD:
                raise BadUpload(f"field '{self._target}' too long")
            self._text += chunk

    def on_part_end(self) -> None:
        if self._target in TEXT_FIELDS:
            self.fields[self._target] = self._text.decode(errors="replace").strip()
        self._target = None

    def close(self, *, delete: bool) -> None:
        if self.file is not None:
            self.file.close()
        if delete and self.path is not None:
            self.path.unlink(missing_ok=True)


async def receive_upload(request: Request, max_bytes: int) -> ParsedUpload:
    content_type, params = parse_options_header(request.headers.get("content-type", ""))
    if content_type != b"multipart/form-data" or b"boundary" not in params:
        raise BadUpload("expected multipart/form-data with a 'file' field")

    sink = _Sink(max_bytes)
    callbacks = {
        name: getattr(sink, name)
        for name in (
            "on_part_begin",
            "on_part_data",
            "on_part_end",
            "on_header_field",
            "on_header_value",
            "on_header_end",
            "on_headers_finished",
        )
    }
    parser = MultipartParser(params[b"boundary"], callbacks)  # type: ignore[arg-type]
    try:
        async for chunk in request.stream():
            parser.write(chunk)
        parser.finalize()
    except (UploadTooLarge, BadUpload):
        sink.close(delete=True)
        raise
    except Exception as e:
        sink.close(delete=True)
        raise BadUpload(f"malformed multipart body: {e}") from e

    sink.close(delete=False)
    if sink.path is None:
        raise BadUpload("missing 'file' field")
    if sink.size == 0:
        sink.path.unlink(missing_ok=True)
        raise BadUpload("empty file")
    return ParsedUpload(
        path=sink.path,
        filename=sink.filename,
        size=sink.size,
        sha256=sink.sha.hexdigest(),
        fields=sink.fields,
    )
