import asyncio

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core import metrics
from app.core.deps import ip_login_limiter
from app.core.ratelimit import LoginThrottle
from app.core.security import create_token, verify_secret
from app.db import models as m
from app.db.session import session_scope

router = APIRouter(prefix="/auth", tags=["auth"])
throttle = LoginThrottle()
_DUMMY = "pbkdf2_sha256$120000$AAAAAAAAAAAAAAAAAAAAAA==$AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


class TokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    client_id: str = Field(min_length=3, max_length=40, pattern=r"^[A-Za-z0-9_\-]+$")
    client_secret: str = Field(min_length=6, max_length=200)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int
    role: str


@router.post("/token", response_model=TokenResponse)
async def issue_token(body: TokenRequest, request: Request):
    ip = request.client.host if request.client else "?"
    ok, _, retry = ip_login_limiter.check(ip)
    if not ok:
        metrics.RATE_LIMITED.labels("login_ip").inc()
        raise HTTPException(429, "too many attempts", headers={"Retry-After": str(int(retry) + 1)})
    lock = throttle.locked(body.client_id)
    if lock > 0:
        metrics.RATE_LIMITED.labels("login_lock").inc()
        raise HTTPException(429, "account temporarily locked after repeated failures", headers={"Retry-After": str(int(lock) + 1)})
    async with session_scope() as s:
        cred = (await s.execute(select(m.ApiCredential).where(m.ApiCredential.client_id == body.client_id))).scalar_one_or_none()
    # always run one hash verification so unknown ids and wrong secrets take the same time (no user-enumeration timing oracle)
    valid = await asyncio.to_thread(verify_secret, body.client_secret, cred.secret_hash if cred else _DUMMY)
    if not cred or not valid:
        throttle.failure(body.client_id)
        raise HTTPException(401, "invalid credentials")
    throttle.success(body.client_id)
    token, ttl = create_token(cred.client_id, cred.role, cred.customer_id)
    return TokenResponse(access_token=token, expires_in=ttl, role=cred.role)
