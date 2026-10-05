"""Tool handlers. All SQL uses SQLAlchemy bound parameters (G-TOOL-05). Every query is scoped with
`customer_id == ctx.customer_id` so a prompt-injected agent still cannot read another customer's rows.
IDs that exist but belong to someone else return the SAME 'not found' message as missing IDs (no enumeration oracle).
Internal fields (risk_flag, fraud scores) are never returned. Action tools require a matching verification (G-TOOL-13)."""
from __future__ import annotations

import json
import random
from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, StringConstraints
from sqlalchemy import select

from app.core.config import get_settings
from app.db import models as m
from app.db.ids import next_id
from app.db.models import utcnow
from app.db.session import session_scope
from app.tools import kg, verify as V
from app.tools.kb import get_kb
from app.tools.rules import usd
from app.tools.runtime import StrictArgs, ToolContext, ToolResult, ToolSpec, business_today, register

TxnId = Annotated[str, StringConstraints(pattern=r"^TXN-\d{8}$")]
TrfId = Annotated[str, StringConstraints(pattern=r"^TRF-\d{8}$")]
CardId = Annotated[str, StringConstraints(pattern=r"^CARD-\d{7}$")]
VerId = Annotated[str, StringConstraints(pattern=r"^VER-\d{6}$")]
NOT_FOUND = "no such record for this customer"


def day(dt: datetime | None) -> str | None:
    return dt.date().isoformat() if dt else None


def _txn_row(t: m.Transaction, cards: dict, mers: dict) -> dict:
    mm = mers.get(t.merchant_id)
    c = cards.get(t.card_id)
    return {"txn_id": t.id, "date": day(t.created_at), "time": t.created_at.strftime("%H:%M"), "description": t.description, "amount": usd(t.amount_cents), "direction": t.direction,
            "kind": t.kind, "status": t.status, "card_last4": c.last4 if c else None, "channel": t.channel, "country": t.country,
            **({"decline_reason": t.decline_reason} if t.decline_reason else {}), **({"merchant_category": mm.category} if mm else {}), **({"linked_txn": t.linked_txn_id} if t.linked_txn_id else {})}


# ------------------------------------------------------------------ common
class KBArgs(StrictArgs):
    query: str = Field(min_length=3, max_length=300, description="Natural-language search query")
    category: Literal["payments", "cards", "general"] | None = Field(None, description="Optional topic filter")


async def h_search_kb(ctx: ToolContext, a: KBArgs) -> ToolResult:
    kb = await get_kb()
    default_cat = {"payments": "payments", "cards": "cards"}.get(ctx.agent, "general")
    hits = await kb.search(a.query, category=a.category or default_cat, k=2)
    if not hits:
        return ToolResult(True, {"results": [], "no_relevant_article": True,
                                 "note": "No relevant article found. Do not guess; say you are unsure or hand off to a human."}, source="kb:none")
    data = {"results": [h.as_dict() for h in hits], "no_relevant_article": False,
            "match_quality": "strong" if hits[0].score >= get_settings().kb_strong_score else "weak"}
    if data["match_quality"] == "weak":
        data["note"] = ("WEAK MATCH: these articles are only loosely related. Use one ONLY if it directly answers the customer's question; "
                        "otherwise say you are not certain, do not infer or generalise from related topics, and set needs_human=true.")
    return ToolResult(True, data, source=["kb:" + h.chunk_id for h in hits])


class NoArgs(StrictArgs):
    pass


async def h_profile(ctx: ToolContext, a: NoArgs) -> ToolResult:
    async with session_scope() as s:
        c = await s.get(m.Customer, ctx.customer_id)
        n_cards = len((await s.execute(select(m.Card.id).where(m.Card.customer_id == ctx.customer_id))).all())
    if not c:
        return ToolResult(False, error="customer not found")
    return ToolResult(True, {"name": c.name, "segment": c.segment, "identity_verified": c.kyc_status == "verified", "country": c.country, "cards": n_cards,
                             "member_since": day(c.created_at)}, source="db:customer")  # risk_flag is INTERNAL and never returned


async def h_accounts(ctx: ToolContext, a: NoArgs) -> ToolResult:
    async with session_scope() as s:
        rows = (await s.execute(select(m.Account).where(m.Account.customer_id == ctx.customer_id).order_by(m.Account.id))).scalars().all()
    out = [{"account_id": r.id, "type": r.type, "last4": r.number_last4, "balance": usd(r.balance_cents), "available": usd(r.balance_cents + r.overdraft_limit_cents) if r.type == "checking" else None,
            "status": r.status} for r in rows]
    return ToolResult(True, {"accounts": out}, source="db:accounts")


class TxnsArgs(StrictArgs):
    days: int = Field(30, ge=1, le=90, description="Look back this many days")
    kind: Literal["card_purchase", "atm_withdrawal", "transfer_out", "transfer_in", "fee", "refund", "provisional_credit", "payroll"] | None = None
    status: Literal["pending", "posted", "reversed", "declined"] | None = None
    merchant: str | None = Field(None, max_length=60, description="Part of the merchant / description")
    min_amount: float | None = Field(None, ge=0, description="USD")
    max_amount: float | None = Field(None, ge=0, description="USD")
    limit: int = Field(12, ge=1, le=30)


async def _lookups(s):
    cards = {c.id: c for c in (await s.execute(select(m.Card).where(m.Card.customer_id.is_not(None)))).scalars().all()}
    return cards


async def h_transactions(ctx: ToolContext, a: TxnsArgs) -> ToolResult:
    today = await business_today()
    since = datetime.combine(today, datetime.min.time()) + timedelta(days=1) - timedelta(days=a.days)
    q = select(m.Transaction).where(m.Transaction.customer_id == ctx.customer_id, m.Transaction.created_at >= since)
    if a.kind:
        q = q.where(m.Transaction.kind == a.kind)
    if a.status:
        q = q.where(m.Transaction.status == a.status)
    if a.merchant:
        q = q.where(m.Transaction.description.ilike(f"%{a.merchant}%"))
    if a.min_amount is not None:
        q = q.where(m.Transaction.amount_cents >= int(round(a.min_amount * 100)))
    if a.max_amount is not None:
        q = q.where(m.Transaction.amount_cents <= int(round(a.max_amount * 100)))
    q = q.order_by(m.Transaction.created_at.desc(), m.Transaction.id.desc()).limit(a.limit)
    async with session_scope() as s:
        rows = (await s.execute(q)).scalars().all()
        cards = {c.id: c for c in (await s.execute(select(m.Card).where(m.Card.customer_id == ctx.customer_id))).scalars().all()}
        mers = {x.id: x for x in (await s.execute(select(m.Merchant))).scalars().all()}
    return ToolResult(True, {"transactions": [_txn_row(t, cards, mers) for t in rows], "count": len(rows), "note": "newest first"}, source="db:transactions")


class TxnArgs(StrictArgs):
    txn_id: TxnId


async def h_txn_detail(ctx: ToolContext, a: TxnArgs) -> ToolResult:
    async with session_scope() as s:
        t = (await s.execute(select(m.Transaction).where(m.Transaction.id == a.txn_id, m.Transaction.customer_id == ctx.customer_id))).scalar_one_or_none()
        if not t:
            return ToolResult(False, error=NOT_FOUND)
        cards = {c.id: c for c in (await s.execute(select(m.Card).where(m.Card.customer_id == ctx.customer_id))).scalars().all()}
        mers = {x.id: x for x in (await s.execute(select(m.Merchant))).scalars().all()}
        related = (await s.execute(select(m.Transaction).where(m.Transaction.customer_id == ctx.customer_id, m.Transaction.linked_txn_id == t.id))).scalars().all()
        disp = (await s.execute(select(m.Dispute).where(m.Dispute.txn_id == t.id, m.Dispute.customer_id == ctx.customer_id))).scalars().all()
    d = _txn_row(t, cards, mers)
    mm = mers.get(t.merchant_id)
    d.update(posted=day(t.posted_at), merchant=mm.name if mm else None, merchant_country=mm.country if mm else None,
             related=[{"txn_id": r.id, "kind": r.kind, "amount": usd(r.amount_cents), "status": r.status} for r in related],
             disputes=[{"dispute_id": x.id, "status": x.status, "reason": x.reason} for x in disp])
    return ToolResult(True, d, source=f"db:transaction:{t.id}")


class TrfArgs(StrictArgs):
    transfer_id: TrfId | None = Field(None, description="Omit to list the most recent transfers")


async def h_transfer_status(ctx: ToolContext, a: TrfArgs) -> ToolResult:
    q = select(m.Transfer).where(m.Transfer.customer_id == ctx.customer_id)
    q = q.where(m.Transfer.id == a.transfer_id) if a.transfer_id else q.order_by(m.Transfer.initiated_at.desc(), m.Transfer.id.desc()).limit(5)
    async with session_scope() as s:
        rows = (await s.execute(q)).scalars().all()
    if a.transfer_id and not rows:
        return ToolResult(False, error=NOT_FOUND)
    return ToolResult(True, {"transfers": [{"transfer_id": r.id, "rail": r.rail, "amount": usd(r.amount_cents), "fee": usd(r.fee_cents), "to": r.to_name, "to_account": r.to_account_masked, "status": r.status,
                                            "return_code": r.return_code, "reference": r.reference, "sent": day(r.initiated_at), "expected_by": day(r.expected_by), "completed": day(r.completed_at)} for r in rows]},
                      source="db:transfers")


async def h_cards(ctx: ToolContext, a: NoArgs) -> ToolResult:
    async with session_scope() as s:
        rows = (await s.execute(select(m.Card).where(m.Card.customer_id == ctx.customer_id).order_by(m.Card.id))).scalars().all()
    return ToolResult(True, {"cards": [{"card_id": c.id, "type": c.type, "network": c.network, "last4": c.last4, "status": c.status, "expiry": c.expiry, "daily_limit": usd(c.daily_limit_cents),
                                        "contactless": bool(c.contactless), "international_payments": bool(c.intl_enabled)} for c in rows]}, source="db:cards")


async def h_fraud_alerts(ctx: ToolContext, a: NoArgs) -> ToolResult:
    async with session_scope() as s:
        rows = (await s.execute(select(m.FraudAlert).where(m.FraudAlert.customer_id == ctx.customer_id).order_by(m.FraudAlert.created_at.desc()).limit(5))).scalars().all()
    return ToolResult(True, {"alerts": [{"alert_id": r.id, "txn_id": r.txn_id, "signal": r.signal, "status": r.status, "date": day(r.created_at)} for r in rows]}, source="db:fraud_alerts")  # score is INTERNAL


class PolicyArgs(StrictArgs):
    topic: str = Field(min_length=2, max_length=60, description="Policy id (POL-DSP-01) or a keyword such as 'dispute', 'fee', 'ACH', 'limits'")


async def h_policy(ctx: ToolContext, a: PolicyArgs) -> ToolResult:
    async with session_scope() as s:
        rows = (await s.execute(select(m.Policy).order_by(m.Policy.id))).scalars().all()
    t = a.topic.lower()
    hit = [r for r in rows if t in r.id.lower() or t in r.title.lower() or t in r.rule.lower() or t in r.regulation.lower()]
    hit = [r for r in hit if r.id not in ("POL-AML-01", "POL-KYC-01")]  # compliance rules are internal
    if not hit:
        return ToolResult(True, {"policies": [], "note": "No matching policy. Do not guess."}, source="policy:none")
    return ToolResult(True, {"policies": [{"policy": r.id, "title": r.title, "rule": r.rule, "params": json.loads(r.params), "regulation": r.regulation, "help_article": r.kb_article, "version": r.version} for r in hit[:4]]},
                      source=[f"policy:{r.id}" for r in hit[:4]])


class KGArgs(StrictArgs):
    entity: str = Field(min_length=3, max_length=40, description="An issue type (duplicate_charge, unrecognised_payment, lost_stolen_card, transfer_trace, transfer_cancel, fee_dispute, declined_payment) or one of the customer's ids (TXN-..., CARD-..., TRF-...)")


async def h_kg(ctx: ToolContext, a: KGArgs) -> ToolResult:
    ent = a.entity.strip()
    if ent.startswith(("TXN-", "CARD-", "TRF-")):
        pref = {"TXN": "txn", "CARD": "card", "TRF": "trf"}[ent.split("-")[0]]
        ent = f"{pref}:{ent}"
    data = await kg.entity_view(ctx.customer_id, ent)
    if "public" in data:  # only the customer-safe view ever reaches the model
        data = {"issue": data["issue"], "actions": data["actions"], **data["public"]}
    return ToolResult(True, data, source=f"kg:{a.entity}")


ISSUES = Literal["duplicate_charge", "unrecognised_payment", "lost_stolen_card", "transfer_trace", "transfer_cancel", "fee_dispute", "declined_payment"]


class VerifyArgs(StrictArgs):
    issue_type: ISSUES
    subject_id: str | None = Field(None, pattern=r"^(TXN-\d{8}|TRF-\d{8}|CARD-\d{7})$", description="The transaction, transfer or card the question is about")


async def h_verify(ctx: ToolContext, a: VerifyArgs) -> ToolResult:
    rep, err = await V.verify(ctx.customer_id, ctx.query_id, a.issue_type, a.subject_id, await business_today())
    if err:
        return ToolResult(False, error=err)
    return ToolResult(True, rep, source=[f"verification:{rep['verification_id']}", "db:" + ",".join(rep["consulted"]["database_tables"]), *[f"policy:{p}" for p in rep["consulted"]["policies"]]])


# ------------------------------------------------------------------ actions (all need a verification)
class DisputeArgs(StrictArgs):
    verification_id: VerId = Field(description="Id returned by verify_transaction_issue (decision must be 'act' and allow file_dispute)")
    note: str = Field("", max_length=200, description="The customer's own words, short")


async def h_file_dispute(ctx: ToolContext, a: DisputeArgs) -> ToolResult:
    today = await business_today()
    d, v, err = await V.require(ctx.customer_id, a.verification_id, "file_dispute", today)
    if err:
        return ToolResult(False, error=err)
    reason = "duplicate" if d.issue_type == "duplicate_charge" else "unauthorized"
    async with session_scope() as s:
        ex = (await s.execute(select(m.Dispute).where(m.Dispute.txn_id == d.subject_id, m.Dispute.customer_id == ctx.customer_id, m.Dispute.status != "rejected"))).scalars().first()
        if ex:  # G-TOOL-09 idempotent
            return ToolResult(True, {"dispute_id": ex.id, "status": ex.status, "amount": usd(ex.amount_cents), "note": "a dispute already exists for this transaction"}, source=f"db:dispute:{ex.id}",
                              requires_human=ex.status == "pending_approval")
        t = await s.get(m.Transaction, d.subject_id)
        did = await next_id(s, "DSP", 6)
        auto = d.approval == "auto"
        prov = None
        if auto:  # provisional credit lands immediately
            tid = await next_id(s, "TXN", 8)
            prov = m.Transaction(id=tid, customer_id=ctx.customer_id, account_id=t.account_id, card_id=None, kind="provisional_credit", direction="credit", amount_cents=d.amount_cents, currency="USD",
                                 merchant_id=None, description="Provisional credit (dispute " + did + ")", status="posted", channel="app", country="US", linked_txn_id=t.id, created_at=utcnow(), posted_at=utcnow())
            s.add(prov)
            acct = await s.get(m.Account, t.account_id)
            acct.balance_cents += d.amount_cents
        disp = m.Dispute(id=did, customer_id=ctx.customer_id, txn_id=t.id, reason=reason, amount_cents=d.amount_cents, status="provisional_credit_issued" if auto else "pending_approval",
                         provisional_txn_id=prov.id if prov else None, verification_id=v.id, created_by=f"agent:{ctx.agent}", created_at=utcnow())
        s.add(disp)
    days = d.book.p("POL-DSP-01", "investigation_business_days")
    out = {"dispute_id": disp.id, "status": disp.status, "amount": usd(disp.amount_cents), "txn_id": disp.txn_id, "reason": reason, "verification_id": v.id,
           "next_step": (f"Provisional credit of {usd(disp.amount_cents)} was posted to the account ({prov.id}). We will investigate and confirm the outcome within {days} business days." if auto else
                         "A human specialist must approve this dispute before any provisional credit is posted (amount or account conditions above the auto-approval limit); usually within 1 business day.")}
    if prov:
        out["provisional_credit_txn"] = prov.id
    return ToolResult(True, out, source=f"db:dispute:{disp.id}", requires_human=not auto)


class FeeArgs(StrictArgs):
    verification_id: VerId


async def h_reverse_fee(ctx: ToolContext, a: FeeArgs) -> ToolResult:
    today = await business_today()
    d, v, err = await V.require(ctx.customer_id, a.verification_id, "reverse_fee", today)
    if err:
        return ToolResult(False, error=err)
    async with session_scope() as s:
        ex = (await s.execute(select(m.Transaction).where(m.Transaction.linked_txn_id == d.subject_id, m.Transaction.customer_id == ctx.customer_id, m.Transaction.kind == "refund"))).scalars().first()
        if ex:
            return ToolResult(True, {"reversal_txn": ex.id, "amount": usd(ex.amount_cents), "status": ex.status, "note": "this fee was already reversed"}, source=f"db:transaction:{ex.id}")
        fee = await s.get(m.Transaction, d.subject_id)
        tid = await next_id(s, "TXN", 8)
        s.add(m.Transaction(id=tid, customer_id=ctx.customer_id, account_id=fee.account_id, card_id=None, kind="refund", direction="credit", amount_cents=fee.amount_cents, currency="USD", merchant_id=None,
                            description="Fee waiver", status="posted", channel="app", country="US", linked_txn_id=fee.id, created_at=utcnow(), posted_at=utcnow()))
        s.add(m.FeeWaiver(customer_id=ctx.customer_id, fee_txn_id=fee.id, waived_at=utcnow()))
        acct = await s.get(m.Account, fee.account_id)
        acct.balance_cents += fee.amount_cents
    return ToolResult(True, {"reversal_txn": tid, "fee_txn": d.subject_id, "amount": usd(d.amount_cents), "status": "posted", "next_step": "The fee was credited back to the account."},
                      source=f"db:transaction:{tid}")


class CancelArgs(StrictArgs):
    verification_id: VerId


async def h_cancel_transfer(ctx: ToolContext, a: CancelArgs) -> ToolResult:
    today = await business_today()
    d, v, err = await V.require(ctx.customer_id, a.verification_id, "cancel_transfer", today)
    if err:
        return ToolResult(False, error=err)
    async with session_scope() as s:
        tr = await s.get(m.Transfer, d.subject_id)
        if tr.status == "cancelled":
            return ToolResult(True, {"transfer_id": tr.id, "status": "cancelled", "note": "already cancelled"}, source=f"db:transfer:{tr.id}")
        tr.status = "cancelled"
        if tr.txn_id:
            tx = await s.get(m.Transaction, tr.txn_id)
            tx.status = "reversed"
    return ToolResult(True, {"transfer_id": tr.id, "status": "cancelled", "amount": usd(tr.amount_cents), "next_step": "The transfer was cancelled before it was sent; the held amount is released."},
                      source=f"db:transfer:{tr.id}")


class BlockArgs(StrictArgs):
    card_id: CardId
    reason: Literal["lost", "stolen", "fraud", "customer_request"] = "customer_request"


async def h_block_card(ctx: ToolContext, a: BlockArgs) -> ToolResult:
    async with session_scope() as s:
        c = (await s.execute(select(m.Card).where(m.Card.id == a.card_id, m.Card.customer_id == ctx.customer_id))).scalar_one_or_none()
        if not c:
            return ToolResult(False, error=NOT_FOUND)
        if c.status in ("blocked", "lost"):
            return ToolResult(True, {"card_id": c.id, "last4": c.last4, "status": c.status, "note": "already blocked"}, source=f"db:card:{c.id}")
        c.status = "lost" if a.reason in ("lost", "stolen") else "blocked"
    return ToolResult(True, {"card_id": c.id, "last4": c.last4, "status": c.status, "next_step": "The card is blocked: no new payments or withdrawals will be authorised. Liability for a card reported before misuse is zero."},
                      source=f"db:card:{c.id}")


class ReplaceArgs(StrictArgs):
    card_id: CardId = Field(description="The blocked / lost / expired card to replace")


async def h_replacement_card(ctx: ToolContext, a: ReplaceArgs) -> ToolResult:
    async with session_scope() as s:
        c = (await s.execute(select(m.Card).where(m.Card.id == a.card_id, m.Card.customer_id == ctx.customer_id))).scalar_one_or_none()
        if not c:
            return ToolResult(False, error=NOT_FOUND)
        if c.status not in ("blocked", "lost", "expired"):
            return ToolResult(False, error=f"the card is {c.status}; a replacement is only issued for a blocked, lost or expired card")
        ex = (await s.execute(select(m.Card).where(m.Card.customer_id == ctx.customer_id, m.Card.account_id == c.account_id, m.Card.status == "pending_activation"))).scalars().first()
        if ex:  # G-TOOL-09 idempotent
            return ToolResult(True, {"new_card_id": ex.id, "last4": ex.last4, "status": ex.status, "note": "a replacement was already ordered"}, source=f"db:card:{ex.id}")
        nid_ = await next_id(s, "CARD", 7)
        today = await business_today()
        new = m.Card(id=nid_, customer_id=ctx.customer_id, account_id=c.account_id, type=c.type, network=c.network, last4=f"{random.Random(nid_).randint(0, 9999):04d}", status="pending_activation",
                     expiry=f"{today.month:02d}/{(today.year + 4) % 100:02d}", daily_limit_cents=c.daily_limit_cents, contactless=c.contactless, intl_enabled=c.intl_enabled, issued_at=datetime.combine(today, datetime.min.time()))
        s.add(new)
    return ToolResult(True, {"new_card_id": new.id, "last4": new.last4, "status": new.status, "delivery": "5-7 business days", "next_step": "Activate the new card in the app when it arrives."}, source=f"db:card:{new.id}")


# ------------------------------------------------------------------ technical / service
async def h_service_status(ctx: ToolContext, a: NoArgs) -> ToolResult:
    async with session_scope() as s:
        rows = (await s.execute(select(m.ServiceComponent).order_by(m.ServiceComponent.name))).scalars().all()
    comps = [{"component": r.name, "status": r.status, "note": r.note} for r in rows]
    return ToolResult(True, {"components": comps, "all_operational": all(c["status"] == "operational" for c in comps)}, source="db:service_status")


class TicketArgs(StrictArgs):
    summary: str = Field(min_length=10, max_length=300)
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    category: Literal["payments", "cards", "account", "other"] = "payments"


async def h_create_ticket(ctx: ToolContext, a: TicketArgs) -> ToolResult:
    async with session_scope() as s:
        dup = (await s.execute(select(m.Ticket).where(m.Ticket.customer_id == ctx.customer_id, m.Ticket.summary == a.summary,
                                                      m.Ticket.created_at >= utcnow() - timedelta(minutes=10)))).scalars().first()
        if dup:  # G-TOOL-09
            return ToolResult(True, {"ticket_id": dup.id, "status": dup.status, "note": "existing ticket reused"}, source=f"db:ticket:{dup.id}")
        tid = await next_id(s, "TCK", 6)
        t = m.Ticket(id=tid, customer_id=ctx.customer_id, summary=a.summary, category=a.category, severity=a.severity, status="open", created_at=utcnow())
        s.add(t)
    return ToolResult(True, {"ticket_id": t.id, "status": "open", "severity": a.severity}, source=f"db:ticket:{t.id}")


# ------------------------------------------------------------------ escalation
class HistoryArgs(StrictArgs):
    limit: int = Field(5, ge=1, le=10)


async def h_ticket_history(ctx: ToolContext, a: HistoryArgs) -> ToolResult:
    async with session_scope() as s:
        rows = (await s.execute(select(m.Ticket).where(m.Ticket.customer_id == ctx.customer_id).order_by(m.Ticket.created_at.desc()).limit(a.limit))).scalars().all()
    return ToolResult(True, {"tickets": [{"ticket_id": t.id, "summary": t.summary, "category": t.category, "severity": t.severity, "status": t.status, "created": day(t.created_at)} for t in rows],
                             "open_or_escalated": sum(1 for t in rows if t.status in ("open", "escalated"))}, source="db:tickets")


ETA = {"standard": "within 1 business day", "premium": "within 4 hours", "private": "within 1 hour"}


class AssignArgs(StrictArgs):
    queue: Literal["payments", "cards", "fraud", "compliance", "escalations", "security"]
    priority: Literal["low", "medium", "high", "critical"]
    reason: str = Field(min_length=5, max_length=300)
    summary: str = Field(min_length=10, max_length=1500)
    customer_message: str = Field(default="", max_length=2000)


async def h_assign_to_human(ctx: ToolContext, a: AssignArgs) -> ToolResult:
    from app.guardrails.pii import mask_pii
    _sens = {"secret", "card", "ssn", "iban"}   # G-IN-03: payment data / secrets are never persisted, even if an LLM-written summary quotes them
    a = a.model_copy(update={"summary": mask_pii(a.summary, kinds=_sens), "customer_message": mask_pii(a.customer_message, kinds=_sens), "reason": mask_pii(a.reason, kinds=_sens)})
    async with session_scope() as s:
        c = await s.get(m.Customer, ctx.customer_id)
        row = (await s.execute(select(m.HumanReview).where(m.HumanReview.query_id == ctx.query_id))).scalars().first()
        if row is None:
            hid = await next_id(s, "HRQ", 6)
            row = m.HumanReview(id=hid, query_id=ctx.query_id, customer_id=ctx.customer_id, priority=a.priority, reason=a.reason, summary=a.summary, customer_message=a.customer_message, status="pending")
            s.add(row)
        else:  # G-TOOL-09: one queue row per query; later calls enrich it
            row.priority, row.reason, row.summary = a.priority, a.reason, a.summary
    eta = ETA.get(c.segment if c else "standard", ETA["standard"])
    return ToolResult(True, {"review_id": row.id, "queue": a.queue, "priority": a.priority, "eta": eta}, source=f"db:review:{row.id}", requires_human=True)


def register_all():
    PAY, CARDS, GEN = "payments", "cards", "general"
    reg = lambda name, desc, model, fn, agents, **kw: register(ToolSpec(name, desc, model, fn, agents, **kw))  # noqa: E731
    reg("search_knowledge_base", "Search the help-center knowledge base.", KBArgs, h_search_kb, {PAY, CARDS, GEN})
    reg("get_customer_profile", "Customer segment, identity-verification status, country.", NoArgs, h_profile, {PAY, CARDS, GEN, "escalation"})
    reg("get_accounts", "The customer's accounts with balances and status.", NoArgs, h_accounts, {PAY, CARDS})
    reg("get_transactions", "Recent ledger transactions (filter by days, kind, status, merchant, amount), newest first.", TxnsArgs, h_transactions, {PAY, CARDS})
    reg("get_transaction_detail", "One transaction in detail, with related credits and disputes.", TxnArgs, h_txn_detail, {PAY, CARDS}, strict_keys={"txn_id"})
    reg("get_transfer_status", "Status of one transfer (or the latest five): rail, expected date, return code.", TrfArgs, h_transfer_status, {PAY}, strict_keys={"transfer_id"})
    reg("get_cards", "The customer's cards: status, limits, controls.", NoArgs, h_cards, {CARDS})
    reg("get_fraud_alerts", "Fraud-monitoring alerts on the customer's account.", NoArgs, h_fraud_alerts, {CARDS})
    reg("get_policy", "Look up bank policy rules by id or keyword (with the regulation and help article).", PolicyArgs, h_policy, {PAY, CARDS, GEN})
    reg("query_knowledge_graph", "Ask the knowledge graph which checks, policies, regulations and actions apply to an issue type, or how one of the customer's ids relates to others.", KGArgs, h_kg, {PAY, CARDS})
    reg("verify_transaction_issue", "VERIFY before acting: investigate a duplicate charge, an unrecognised payment, a lost card, a transfer, a fee or a decline against the ledger, policies and knowledge graph. Returns a decision and a verification_id.",
        VerifyArgs, h_verify, {PAY, CARDS}, strict_keys={"subject_id"})
    reg("file_dispute", "File a dispute (duplicate or unauthorised) with provisional credit when allowed. Needs verification_id from verify_transaction_issue.", DisputeArgs, h_file_dispute, {PAY, CARDS}, write=True, strict_keys={"verification_id"})
    reg("reverse_fee", "Reverse a fee as a courtesy waiver. Needs verification_id.", FeeArgs, h_reverse_fee, {PAY}, write=True, strict_keys={"verification_id"})
    reg("cancel_transfer", "Cancel a pending transfer that has not been submitted. Needs verification_id.", CancelArgs, h_cancel_transfer, {PAY}, write=True, strict_keys={"verification_id"})
    reg("block_card", "Block a card immediately (lost, stolen, fraud). Protective: allowed on the customer's request.", BlockArgs, h_block_card, {CARDS}, write=True, strict_keys={"card_id"})
    reg("request_replacement_card", "Order a replacement for a blocked, lost or expired card.", ReplaceArgs, h_replacement_card, {CARDS}, write=True, strict_keys={"card_id"})
    reg("get_service_status", "Live status of Orbit Bank channels (app, online banking, card network, rails, ATMs).", NoArgs, h_service_status, {PAY, CARDS})
    reg("create_ticket", "Open a follow-up ticket (only when the customer asks for one).", TicketArgs, h_create_ticket, {PAY, CARDS}, write=True)
    reg("get_ticket_history", "The customer's recent support tickets.", HistoryArgs, h_ticket_history, {"escalation"})
    reg("assign_to_human", "Hand the case to a human specialist queue.", AssignArgs, h_assign_to_human, {"escalation"}, write=True)


register_all()
