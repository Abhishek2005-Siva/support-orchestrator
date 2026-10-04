from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.deps import User, require_roles
from app.db import models as m
from app.db.session import session_scope
from app.graph import runner
from app.integrations.slack import notify_resolution

router = APIRouter(prefix="/v1/review-queue", tags=["review"])
staff = require_roles("agent_staff", "admin")


class ResolveBody(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal["approve", "edit", "reject"]
    reply: str | None = Field(None, min_length=3, max_length=2000)
    note: str | None = Field(None, max_length=500)


def _row(r: m.HumanReview) -> dict:
    return {"review_id": r.id, "query_id": r.query_id, "customer_id": r.customer_id, "priority": r.priority, "reason": r.reason,
            "summary": r.summary, "customer_message": r.customer_message, "draft_reply": r.draft_reply, "holding_reply": r.holding_reply,
            "status": r.status, "reviewer": r.reviewer, "created_at": r.created_at.isoformat(), "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None}


@router.get("")
async def list_queue(status: Literal["pending", "approved", "edited", "rejected", "all"] = "pending", limit: int = Query(50, ge=1, le=200),
                     user: User = Depends(staff)):
    order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    async with session_scope() as s:
        q = select(m.HumanReview).order_by(m.HumanReview.created_at).limit(limit * 3)
        if status != "all":
            q = q.where(m.HumanReview.status == status)
        rows = (await s.execute(q)).scalars().all()
    rows.sort(key=lambda r: (order.get(r.priority, 9), r.created_at))
    return {"count": len(rows[:limit]), "items": [_row(r) for r in rows[:limit]]}


@router.get("/{review_id}")
async def get_item(review_id: str, user: User = Depends(staff)):
    async with session_scope() as s:
        r = await s.get(m.HumanReview, review_id)
    if not r:
        raise HTTPException(404, "review not found")
    return _row(r)


@router.post("/{review_id}/resolve")
async def resolve(review_id: str, body: ResolveBody, user: User = Depends(staff)):
    if body.action == "edit" and not body.reply:
        raise HTTPException(422, "`reply` is required for action=edit")
    try:
        res = await runner.resume_review(review_id=review_id, action=body.action, reviewer=user.sub, reply=body.reply, note=body.note)
    except KeyError:
        raise HTTPException(404, "review not found")
    except ValueError as e:
        raise HTTPException(409, str(e))
    await notify_resolution(res["query_id"], res["reply"], res["status"])
    return res
