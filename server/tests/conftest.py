"""Test fixtures: a real Postgres (testcontainers), migrated with Alembic, and an
httpx client bound to the ASGI app."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from testcontainers.community.postgres import PostgresContainer

ADMIN_PASSWORD = "test-admin-password"
SUBSONIC_USER = "tester"
SUBSONIC_PASSWORD = "sesame"

SERVER_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    external = os.environ.get("TEST_DATABASE_URL")
    if external:  # e.g. a compose test service in CI
        yield external
        return
    with PostgresContainer("postgres:16-alpine", driver="asyncpg") as pg:
        yield pg.get_connection_url()


@pytest.fixture(scope="session")
def music_dir() -> Iterator[Path]:
    d = Path(tempfile.mkdtemp(prefix="soundable-test-"))
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="session", autouse=True)
def _settings_env(postgres_url: str, music_dir: Path) -> Iterator[None]:
    env = {
        "DATABASE_URL": postgres_url,
        "MUSIC_DIR": str(music_dir),
        "ADMIN_PASSWORD": ADMIN_PASSWORD,
        "SECRET_KEY": "test-secret-key-test-secret-key-0123456789",
        "SUBSONIC_USER": SUBSONIC_USER,
        "SUBSONIC_PASSWORD": SUBSONIC_PASSWORD,
        "LOGIN_MAX_FAILURES": "5",
    }
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    from app.config import get_settings

    get_settings.cache_clear()

    from alembic.config import Config

    from alembic import command

    cfg = Config(str(SERVER_DIR / "alembic.ini"))
    cfg.attributes["configure_logger"] = False
    command.upgrade(cfg, "head")
    yield
    for k, v in old.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


@pytest.fixture(scope="session")
async def app():  # type: ignore[no-untyped-def]
    from app.main import create_app

    application = create_app()
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app) -> AsyncIterator:  # type: ignore[no-untyped-def]
    import httpx

    from app.security import login_limiter

    login_limiter().reset("127.0.0.1")
    transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def token(client) -> str:  # type: ignore[no-untyped-def]
    r = await client.post("/api/auth/login", json={"password": ADMIN_PASSWORD, "name": "pytest"})
    assert r.status_code == 200, r.text
    return r.json()["token"]


@pytest.fixture
async def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}
