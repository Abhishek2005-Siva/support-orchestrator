"""Security-event + audit logging. Every blocked call / detected attack is written to
  (1) the security_events table, (2) logs/security_events.jsonl (easy to eyeball / grep), (3) the active trace.
Never raises: logging must not take the request down. Guardrail id: G-OBS-01."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from app.core.config import get_settings
from app.db import models as m
from app.db.session import session_scope
from app.guardrails.pii import mask_pii
from app.observability.tracing import hash_id, tracer


async def log_security_event(kind: str, detail: str, *, query_id: str | None = None, customer_id: str | None = None,
                             blocked: bool = True) -> None:
    detail = mask_pii(detail)[:800]
    tracer.event(f"security.{kind}", detail=detail, blocked=blocked)
    try:
        d = get_settings().log_dir
        d.mkdir(parents=True, exist_ok=True)
        with (d / "security_events.jsonl").open("a") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, "query_id": query_id,
                                "customer": hash_id(customer_id) if customer_id else None, "blocked": blocked,
                                "detail": detail}) + "\n")
    except Exception:
        pass
    try:
        async with session_scope() as s:
            s.add(m.SecurityEvent(kind=kind, query_id=query_id, customer_id=customer_id, detail=detail, blocked=int(blocked)))
    except Exception:
        pass


async def audit(actor: str, action: str, *, query_id: str | None, customer_id: str | None, args: dict | None,
                outcome: str) -> None:
    try:
        async with session_scope() as s:
            s.add(m.AuditLog(query_id=query_id, actor=actor, action=action, customer_id=customer_id,
                             args=mask_pii(json.dumps(args or {}, default=str))[:1500], outcome=outcome))
    except Exception:
        pass
