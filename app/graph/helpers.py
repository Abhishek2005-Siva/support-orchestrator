"""Small DB helpers used by graph nodes (all parameterised SQLAlchemy)."""
from __future__ import annotations

from sqlalchemy import func, select

from app.db import models as m
from app.db.models import utcnow
from app.db.ids import next_id
from app.db.session import session_scope
from app.guardrails.events import audit


async def count_open_tickets(customer_id: str) -> int:
    async with session_scope() as s:
        return (await s.execute(select(func.count()).select_from(m.Ticket).where(
            m.Ticket.customer_id == customer_id, m.Ticket.status.in_(("open", "escalated"))))).scalar_one()


async def get_review(query_id: str) -> m.HumanReview | None:
    async with session_scope() as s:
        return (await s.execute(select(m.HumanReview).where(m.HumanReview.query_id == query_id))).scalars().first()


async def get_review_by_id(review_id: str) -> m.HumanReview | None:
    async with session_scope() as s:
        return await s.get(m.HumanReview, review_id)


async def set_review_draft(query_id: str, draft: str | None, holding: str | None, reason: str | None = None, summary: str | None = None):
    async with session_scope() as s:
        row = (await s.execute(select(m.HumanReview).where(m.HumanReview.query_id == query_id))).scalars().first()
        if row:
            if draft is not None:
                row.draft_reply = draft
            if holding is not None:
                row.holding_reply = holding
            if reason and reason not in (row.reason or ""):
                row.reason = f"{row.reason}; {reason}"[:300] if row.reason else reason[:300]
            if summary and not row.summary:
                row.summary = summary


async def file_approval_review(*, query_id: str, customer_id: str, message: str, summary: str, reply: str) -> str:
    """Refund over the auto-approval limit: a human billing specialist must approve. The customer still gets their
    (truthful) reply, so this does NOT block the conversation. Idempotent per query_id."""
    async with session_scope() as s:
        row = (await s.execute(select(m.HumanReview).where(m.HumanReview.query_id == query_id))).scalars().first()
        if row:
            return row.id
        hid = await next_id(s, "HRQ", 6)
        row = m.HumanReview(id=hid, query_id=query_id, customer_id=customer_id, priority="medium",
                            reason="refund above auto-approval limit: needs human approval", summary=summary[:1500],
                            customer_message=message[:2000], draft_reply=reply, status="pending")
        s.add(row)
    await audit("graph", "file_approval_review", query_id=query_id, customer_id=customer_id, args={"review_id": row.id}, outcome="pending_approval")
    return row.id


async def settle_pending_refunds(customer_id: str, approved: bool, reviewer: str) -> list[m.RefundRequest]:
    """Staff decision on a refund-approval case: move this customer's pending refund requests to approved / rejected (audited)."""
    async with session_scope() as s:
        rows = (await s.execute(select(m.RefundRequest).where(m.RefundRequest.customer_id == customer_id, m.RefundRequest.status == "pending_approval"))).scalars().all()
        for r in rows:
            r.status = "approved" if approved else "rejected"
    for r in rows:
        await audit(f"staff:{reviewer}", "refund_decision", query_id=None, customer_id=customer_id, args={"refund_id": r.id, "decision": r.status}, outcome="ok")
    return rows


async def resolve_review_row(review_id: str, *, status: str, reviewer: str, note: str | None, final_reply: str | None) -> m.HumanReview | None:
    async with session_scope() as s:
        row = await s.get(m.HumanReview, review_id)
        if not row or row.status != "pending":
            return row
        row.status, row.reviewer, row.resolution_note, row.final_reply, row.resolved_at = status, reviewer, note, final_reply, utcnow()
        return row


async def upsert_conversation(query_id: str, customer_id: str, channel: str, message: str, *, status: str = "processing",
                              final_reply: str | None = None, result_json: str | None = None, latency_ms: int | None = None,
                              channel_ref: str | None = None):
    from app.guardrails.pii import mask_pii
    async with session_scope() as s:
        row = await s.get(m.Conversation, query_id)
        if row is None:
            row = m.Conversation(id=query_id, customer_id=customer_id, channel=channel, message=mask_pii(message)[:2000], status=status)
            s.add(row)
        row.status = status
        if channel_ref:
            row.channel_ref = channel_ref
        if final_reply is not None:
            row.final_reply = final_reply
        if result_json is not None:
            row.result_json = result_json
        if latency_ms is not None:
            row.latency_ms = latency_ms
