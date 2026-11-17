"""Shared FastAPI dependencies."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import ApiToken
from app.security import hash_token

SessionDep = Annotated[AsyncSession, Depends(get_session)]

bearer_scheme = HTTPBearer(auto_error=False)


def client_ip(request: Request) -> str:
    """Real client IP. Behind Cloudflare Tunnel the container is only reachable through
    cloudflared, so CF-Connecting-IP can be trusted; otherwise fall back to the
    (proxy-header-aware) peer address."""
    return request.headers.get("cf-connecting-ip") or (
        request.client.host if request.client else "unknown"
    )


async def require_token(
    session: SessionDep,
    creds: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> ApiToken:
    if creds is None or creds.scheme.lower() != "bearer":
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    token = await session.scalar(
        select(ApiToken).where(
            ApiToken.token_hash == hash_token(creds.credentials),
            ApiToken.revoked_at.is_(None),
        )
    )
    if token is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Invalid or revoked token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    # Avoid a write on every request: only bump last_used_at once a minute.
    now = datetime.now(UTC)
    if token.last_used_at is None or now - token.last_used_at > timedelta(minutes=1):
        await session.execute(
            update(ApiToken).where(ApiToken.id == token.id).values(last_used_at=now)
        )
        await session.commit()
    return token


TokenDep = Annotated[ApiToken, Depends(require_token)]
