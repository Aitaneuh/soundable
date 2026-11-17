from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from app.deps import SessionDep, TokenDep, client_ip
from app.models import ApiToken
from app.security import hash_token, login_limiter, new_api_token, verify_admin_password

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginIn(BaseModel):
    password: str = Field(min_length=1, max_length=1024)
    name: str = Field(default="client", max_length=100, description="Label for this token")


class LoginOut(BaseModel):
    token: str
    token_id: str


class MeOut(BaseModel):
    token_id: str
    name: str
    created_at: datetime


@router.post("/login")
async def login(body: LoginIn, request: Request, session: SessionDep) -> LoginOut:
    ip = client_ip(request)
    limiter = login_limiter()
    if limiter.blocked(ip):
        log.warning("login blocked by rate limit ip=%s", ip)
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "Too many failed login attempts, try again later",
            headers={"Retry-After": str(limiter.window)},
        )
    if not await run_in_threadpool(verify_admin_password, body.password):
        limiter.record_failure(ip)
        log.warning("failed login ip=%s", ip)
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Wrong password")

    limiter.reset(ip)
    raw = new_api_token()
    token = ApiToken(token_hash=hash_token(raw), name=body.name)
    session.add(token)
    await session.commit()
    log.info("login ok ip=%s token_id=%s name=%s", ip, token.id, body.name)
    return LoginOut(token=raw, token_id=str(token.id))


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(token: TokenDep, session: SessionDep) -> None:
    token = await session.merge(token)
    token.revoked_at = datetime.now(UTC)
    await session.commit()


@router.get("/me")
async def me(token: TokenDep) -> MeOut:
    return MeOut(token_id=str(token.id), name=token.name, created_at=token.created_at)
