from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import get_engine
from app.library import cleanup_stale_tmp
from app.paths import ForgivingPathMiddleware
from app.routers import auth, health, imports, playlists, root, tracks
from app.security import init_password_hash
from app.subsonic.response import SERVER_VERSION
from app.subsonic.router import router as subsonic_router


def configure_logging() -> None:
    logging.basicConfig(
        level=get_settings().log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = get_settings()
    settings.music_dir.mkdir(parents=True, exist_ok=True)
    cleanup_stale_tmp()
    init_password_hash()
    yield
    await get_engine().dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="soundable", version=SERVER_VERSION, lifespan=lifespan)
    app.include_router(root.router)
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(tracks.router)
    app.include_router(imports.router)
    app.include_router(playlists.router)
    app.include_router(subsonic_router)
    app.add_middleware(ForgivingPathMiddleware)
    return app


app = create_app()
