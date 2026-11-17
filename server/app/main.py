from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import get_settings
from app.db import get_engine
from app.routers import auth, health
from app.security import init_password_hash


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
    init_password_hash()
    yield
    await get_engine().dispose()


def create_app() -> FastAPI:
    app = FastAPI(title="soundable", version="0.1.0", lifespan=lifespan)
    app.include_router(health.router)
    app.include_router(auth.router)
    return app


app = create_app()
