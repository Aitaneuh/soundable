"""Subsonic authentication: u + p (plain or enc:hex) or u + t + s (t = md5(password + s))."""

from __future__ import annotations

import hashlib
import hmac
import logging

from app.config import get_settings
from app.security import login_limiter
from app.subsonic.response import SubsonicError

log = logging.getLogger(__name__)


def _decode_password(p: str) -> str:
    if p.startswith("enc:"):
        try:
            return bytes.fromhex(p[4:]).decode("utf-8")
        except ValueError:
            return ""
    return p


def check_credentials(params: dict[str, str], ip: str) -> None:
    """Raise SubsonicError unless the request carries valid credentials."""
    limiter = login_limiter()
    key = f"subsonic:{ip}"
    user = params.get("u")
    if params.get("apiKey"):
        raise SubsonicError(42, "Provided authentication mechanism not supported (use u/t/s)")
    if not user:
        raise SubsonicError(10, "Required parameter is missing: u")
    if not (params.get("p") or (params.get("t") and params.get("s"))):
        raise SubsonicError(10, "Required parameter is missing: p or t+s")
    if limiter.blocked(key):
        log.warning("subsonic auth blocked by rate limit ip=%s", ip)
        raise SubsonicError(40, "Too many failed attempts, try again later")

    s = get_settings()
    password = s.subsonic_password.get_secret_value()
    user_ok = hmac.compare_digest(user.casefold().encode(), s.subsonic_user.casefold().encode())
    if params.get("t") and params.get("s"):
        expected = hashlib.md5((password + params["s"]).encode()).hexdigest()
        pw_ok = hmac.compare_digest(expected, params["t"].lower())
    else:
        pw_ok = hmac.compare_digest(_decode_password(params["p"]).encode(), password.encode())

    if not (user_ok and pw_ok):
        limiter.record_failure(key)
        log.warning("failed subsonic login ip=%s user=%r client=%r", ip, user, params.get("c"))
        raise SubsonicError(40, "Wrong username or password")
