from __future__ import annotations

import time

from app.security import FailureLimiter, sign_stream, verify_stream_signature
from tests.conftest import ADMIN_PASSWORD


async def test_health(client) -> None:
    r = await client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "db": "ok"}


async def test_login_wrong_password(client) -> None:
    r = await client.post("/api/auth/login", json={"password": "nope"})
    assert r.status_code == 401


async def test_login_and_me(client, auth) -> None:
    r = await client.get("/api/auth/me", headers=auth)
    assert r.status_code == 200
    assert r.json()["name"] == "pytest"


async def test_me_requires_token(client) -> None:
    assert (await client.get("/api/auth/me")).status_code == 401
    r = await client.get("/api/auth/me", headers={"Authorization": "Bearer snd_bogus"})
    assert r.status_code == 401


async def test_logout_revokes_token(client, auth) -> None:
    assert (await client.post("/api/auth/logout", headers=auth)).status_code == 204
    assert (await client.get("/api/auth/me", headers=auth)).status_code == 401


async def test_login_rate_limited(client) -> None:
    # LOGIN_MAX_FAILURES=5 in tests
    for _ in range(5):
        assert (await client.post("/api/auth/login", json={"password": "x"})).status_code == 401
    r = await client.post("/api/auth/login", json={"password": ADMIN_PASSWORD})
    assert r.status_code == 429
    assert "retry-after" in r.headers


def test_failure_limiter_window() -> None:
    lim = FailureLimiter(max_failures=2, window_seconds=0)
    lim.record_failure("a")
    lim.record_failure("a")
    assert not lim.blocked("a")  # zero-length window: everything already expired


def test_stream_signature() -> None:
    exp, sig = sign_stream("abc", ttl=60)
    assert verify_stream_signature("abc", exp, sig)
    assert not verify_stream_signature("abd", exp, sig)
    assert not verify_stream_signature("abc", exp + 1, sig)
    assert not verify_stream_signature("abc", int(time.time()) - 1, sig)


async def test_info(client, auth) -> None:
    r = await client.get("/api/info", headers=auth)
    assert r.status_code == 200 and r.json()["max_upload_mb"] > 0
    assert (await client.get("/api/info")).status_code == 401
