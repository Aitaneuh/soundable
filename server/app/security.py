"""Password hashing, API tokens, signed stream URLs and a login rate limiter."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from functools import lru_cache

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

from app.config import get_settings

_hasher = PasswordHasher()


@lru_cache
def _admin_password_hash() -> str:
    """The admin password is hashed once at startup; only the hash is used afterwards."""
    return _hasher.hash(get_settings().admin_password.get_secret_value())


def init_password_hash() -> None:
    _admin_password_hash()


def verify_admin_password(password: str) -> bool:
    try:
        return _hasher.verify(_admin_password_hash(), password)
    except VerificationError:
        return False


# --- API tokens ---------------------------------------------------------------------

TOKEN_PREFIX = "snd_"


def new_api_token() -> str:
    return TOKEN_PREFIX + secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# --- Signed stream URLs ---------------------------------------------------------------


def _stream_mac(track_id: str, exp: int) -> str:
    key = get_settings().secret_key.get_secret_value().encode()
    msg = f"stream:{track_id}:{exp}".encode()
    return hmac.new(key, msg, hashlib.sha256).hexdigest()[:32]


def sign_stream(track_id: str, ttl: int | None = None) -> tuple[int, str]:
    exp = int(time.time()) + (ttl or get_settings().stream_url_ttl_seconds)
    return exp, _stream_mac(track_id, exp)


def verify_stream_signature(track_id: str, exp: int, sig: str) -> bool:
    if exp < time.time():
        return False
    return hmac.compare_digest(_stream_mac(track_id, exp), sig)


# --- Rate limiting ----------------------------------------------------------------------


class FailureLimiter:
    """Counts failed attempts per key in a sliding window (in memory, single process)."""

    def __init__(self, max_failures: int, window_seconds: int) -> None:
        self.max_failures = max_failures
        self.window = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._failures[key]
        while q and q[0] <= now - self.window:
            q.popleft()
        return q

    def blocked(self, key: str) -> bool:
        return len(self._prune(key, time.monotonic())) >= self.max_failures

    def record_failure(self, key: str) -> None:
        now = time.monotonic()
        self._prune(key, now).append(now)

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)


@lru_cache
def login_limiter() -> FailureLimiter:
    s = get_settings()
    return FailureLimiter(s.login_max_failures, s.login_window_seconds)
