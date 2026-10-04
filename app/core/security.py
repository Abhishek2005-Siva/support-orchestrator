"""Auth primitives: PBKDF2 secret hashing, constant-time verification, JWT issue/verify.
Guardrail ids: G-API-01 (JWT w/ short expiry + roles), G-API-02 (constant-time secret compare)."""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

import jwt

from app.core.config import get_settings

_ITER = 120_000


def hash_secret(secret: str, *, iterations: int = _ITER) -> str:
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_secret(secret: str, stored: str) -> bool:
    try:
        _, it, salt, dk = stored.split("$")
        calc = hashlib.pbkdf2_hmac("sha256", secret.encode(), base64.b64decode(salt), int(it))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


def create_token(sub: str, role: str, customer_id: str | None) -> tuple[str, int]:
    s = get_settings()
    ttl = s.jwt_ttl_minutes * 60
    now = int(time.time())
    tok = jwt.encode({"sub": sub, "role": role, "cid": customer_id, "iat": now, "exp": now + ttl},
                     s.jwt_secret, algorithm=s.jwt_algorithm)
    return tok, ttl


def decode_token(token: str) -> dict:
    s = get_settings()
    return jwt.decode(token, s.jwt_secret, algorithms=[s.jwt_algorithm], options={"require": ["exp", "sub", "role"]})
