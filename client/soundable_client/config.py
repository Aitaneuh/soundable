"""Client configuration (platform config dir) and token storage (keyring, or a 0600 file)."""

from __future__ import annotations

import contextlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import platformdirs

log = logging.getLogger(__name__)

APP_NAME = "soundable"
KEYRING_SERVICE = "soundable"


def config_dir() -> Path:
    override = os.environ.get("SOUNDABLE_CONFIG_DIR")
    d = (
        Path(override)
        if override
        else Path(platformdirs.user_config_dir(APP_NAME, appauthor=False))
    )
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_dir() -> Path:
    override = os.environ.get("SOUNDABLE_CONFIG_DIR")
    d = Path(override) if override else Path(platformdirs.user_state_dir(APP_NAME, appauthor=False))
    d.mkdir(parents=True, exist_ok=True)
    return d


CONFIG_FILE = "config.json"
TOKEN_FILE = "token"


@dataclass
class Config:
    server_url: str = ""
    volume: int = 70
    mpv_path: str = ""  # empty = auto-detect
    history: list[str] = field(default_factory=list)

    @classmethod
    def load(cls) -> Config:
        path = config_dir() / CONFIG_FILE
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)

    def save(self) -> None:
        path = config_dir() / CONFIG_FILE
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")
        os.replace(tmp, path)


class InvalidServerUrl(ValueError):
    pass


def normalize_server_url(raw: str) -> str:
    """Validate and normalise the server URL. HTTPS is required except for localhost."""
    url = raw.strip().rstrip("/")
    if "://" not in url:
        host = url.split("/")[0].split(":")[0]
        url = ("http://" if host in {"localhost", "127.0.0.1", "::1"} else "https://") + url
    p = urlparse(url)
    if p.scheme not in {"http", "https"} or not p.hostname:
        raise InvalidServerUrl("expected something like https://soundable.example.com")
    if p.scheme == "http" and p.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise InvalidServerUrl("HTTPS is required (plain HTTP is only allowed for localhost)")
    if p.query or p.fragment:
        raise InvalidServerUrl("the URL must not contain a query string or fragment")
    return url


# --- Token storage ------------------------------------------------------------------


def _keyring():  # type: ignore[no-untyped-def]
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring

        if isinstance(keyring.get_keyring(), FailKeyring):
            return None
        return keyring
    except Exception:
        return None


def save_token(server_url: str, token: str) -> str:
    """Returns where the token was stored ("keyring" or the file path)."""
    kr = _keyring()
    if kr is not None:
        try:
            kr.set_password(KEYRING_SERVICE, server_url, token)
            (config_dir() / TOKEN_FILE).unlink(missing_ok=True)
            return "keyring"
        except Exception as e:
            log.warning("keyring unavailable (%s), falling back to a file", e)
    path = config_dir() / TOKEN_FILE
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump({"server_url": server_url, "token": token}, f)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)
    return str(path)


def load_token(server_url: str) -> str | None:
    kr = _keyring()
    if kr is not None:
        try:
            tok = kr.get_password(KEYRING_SERVICE, server_url)
            if tok:
                return tok
        except Exception:
            pass
    try:
        data = json.loads((config_dir() / TOKEN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data.get("token") if data.get("server_url") == server_url else None


def delete_token(server_url: str) -> None:
    kr = _keyring()
    if kr is not None:
        with contextlib.suppress(Exception):
            kr.delete_password(KEYRING_SERVICE, server_url)
    (config_dir() / TOKEN_FILE).unlink(missing_ok=True)
