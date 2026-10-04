"""FastAPI dependencies: JWT auth (G-API-01), role guards, per-user rate limit (G-API-03)."""
from __future__ import annotations

from dataclasses import dataclass

import jwt
from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core import metrics
from app.core.config import get_settings
from app.core.ratelimit import SlidingWindowLimiter
from app.core.security import decode_token

bearer = HTTPBearer(auto_error=False)
user_limiter = SlidingWindowLimiter(100)
ip_login_limiter = SlidingWindowLimiter(20)  # /auth/token attempts per IP per minute


def configure_limits():
    user_limiter.limit = get_settings().rate_limit_per_min
    ip_login_limiter.limit = get_settings().login_rate_limit_per_min_ip


@dataclass
class User:
    sub: str
    role: str
    customer_id: str | None


async def current_user(request: Request, response: Response, cred: HTTPAuthorizationCredentials | None = Depends(bearer)) -> User:
    if cred is None or cred.scheme.lower() != "bearer":
        raise HTTPException(401, "missing bearer token", headers={"WWW-Authenticate": "Bearer"})
    try:
        claims = decode_token(cred.credentials)
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "token expired", headers={"WWW-Authenticate": "Bearer"})
    except jwt.PyJWTError:
        raise HTTPException(401, "invalid token", headers={"WWW-Authenticate": "Bearer"})
    user = User(sub=claims["sub"], role=claims["role"], customer_id=claims.get("cid"))
    ok, remaining, retry = user_limiter.check(user.sub)
    response.headers["X-RateLimit-Limit"] = str(user_limiter.limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    if not ok:
        metrics.RATE_LIMITED.labels("user").inc()
        raise HTTPException(429, "rate limit exceeded", headers={"Retry-After": str(int(retry) + 1), "X-RateLimit-Limit": str(user_limiter.limit), "X-RateLimit-Remaining": "0"})
    request.state.user = user
    return user


def require_roles(*roles: str):
    async def dep(user: User = Depends(current_user)) -> User:
        if user.role not in roles:
            raise HTTPException(403, "insufficient role")
        return user
    return dep


async def customer_user(user: User = Depends(current_user)) -> User:
    if user.role != "customer" or not user.customer_id:
        raise HTTPException(403, "customer token required")
    return user
