from __future__ import annotations

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from app.deps import SessionDep

router = APIRouter(prefix="/api", tags=["health"])


@router.get("/health")
async def health(session: SessionDep, response: Response) -> dict[str, str]:
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "error", "db": "unreachable"}
    return {"status": "ok", "db": "ok"}
