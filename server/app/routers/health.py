from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.config import get_settings
from app.deps import SessionDep, TokenDep
from app.subsonic.response import SERVER_VERSION

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health(session: SessionDep, response: Response) -> dict[str, str]:
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "error", "db": "unreachable"}
    return {"status": "ok", "db": "ok"}


@router.get("/info")
async def info(_: TokenDep) -> dict[str, object]:
    """Server limits the client should know about before uploading."""
    return {"version": SERVER_VERSION, "max_upload_mb": get_settings().max_upload_mb}
