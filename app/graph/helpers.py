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
    """Dispute that needs approval (provisional credit above the auto limit or conditions not met): a human specialist must approve. The customer still gets their
    (truthful) reply, so this does NOT block the conversation. Idempotent per query_id."""
    async with session_scope() as s:
        row = (await s.execute(select(m.HumanReview).where(m.HumanReview.query_id == query_id))).scalars().first()
        if row:
            return row.id
        hid = await next_id(s, "HRQ", 6)
        row = m.HumanReview(id=hid, query_id=query_id, customer_id=customer_id, priority="medium",
                            reason="dispute approval needed: provisional credit requires human approval", summary=summary[:1500],
                            customer_message=message[:2000], draft_reply=reply, status="pending")
        s.add(row)
    await audit("graph", "file_approval_review", query_id=query_id, customer_id=customer_id, args={"review_id": row.id}, outcome="pending_approval")
    return row.id


async def settle_pending_disputes(customer_id: str, approved: bool, reviewer: str) -> list[m.Dispute]:
    """Staff decision on a dispute-approval case: approve = post the provisional credit and move to under_review; reject = close it (audited)."""
    out = []
    async with session_scope() as s:
        rows = (await s.execute(select(m.Dispute).where(m.Dispute.customer_id == customer_id, m.Dispute.status == "pending_approval"))).scalars().all()
        for d in rows:
            if approved:
                tid = await next_id(s, "TXN", 8)
                t = await s.get(m.Transaction, d.txn_id)
                s.add(m.Transaction(id=tid, customer_id=customer_id, account_id=t.account_id, kind="provisional_credit", direction="credit", amount_cents=d.amount_cents, currency="USD",
                                    description=f"Provisional credit (dispute {d.id})", status="posted", channel="app", country="US", linked_txn_id=t.id, created_at=utcnow(), posted_at=utcnow()))
                acct = await s.get(m.Account, t.account_id)
                acct.balance_cents += d.amount_cents
                d.status, d.provisional_txn_id = "provisional_credit_issued", tid
            else:
                d.status = "rejected"
            out.append(d)
    for d in out:
        await audit(f"staff:{reviewer}", "dispute_decision", query_id=None, customer_id=customer_id, args={"dispute_id": d.id, "decision": d.status}, outcome="ok")
    return out


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
