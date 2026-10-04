"""Tool handlers. All SQL uses SQLAlchemy bound parameters (G-TOOL-05). Every query is scoped with
`customer_id == ctx.customer_id` so a prompt-injected agent still cannot read another customer's rows.
IDs that exist but belong to someone else return the SAME 'not found' message as missing IDs (no enumeration oracle)."""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Literal

from pydantic import Field, StringConstraints
from sqlalchemy import func, select

from app.core.config import get_settings
from app.db import models as m
from app.db.models import utcnow
from app.db.ids import next_id
from app.db.session import session_scope
from app.tools import rules
from app.tools.kb import get_kb
from app.tools.runtime import StrictArgs, ToolContext, ToolResult, ToolSpec, business_today, register

InvoiceId = Annotated[str, StringConstraints(pattern=r"^INV-\d{8}$")]


def usd(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def day(dt: datetime | None) -> str | None:
    return dt.date().isoformat() if dt else None


NOT_FOUND = "no such invoice for this customer"


# ------------------------------------------------------------------ common
class KBArgs(StrictArgs):
    query: str = Field(min_length=3, max_length=300, description="Natural-language search query")
    category: Literal["billing", "technical", "general"] | None = Field(None, description="Optional topic filter")


async def h_search_kb(ctx: ToolContext, a: KBArgs) -> ToolResult:
    kb = await get_kb()
    default_cat = {"billing": "billing", "technical": "technical"}.get(ctx.agent, "general")
    hits = await kb.search(a.query, category=a.category or default_cat, k=2)
    if not hits:
        return ToolResult(True, {"results": [], "no_relevant_article": True,
                                 "note": "No relevant article found. Do not guess; say you are unsure or hand off to a human."},
                          source="kb:none")
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
    if not c:
        return ToolResult(False, error="customer not found")
    today = await business_today()
    expired = False
    if c.card_expiry:
        mm, yy = c.card_expiry.split("/")
        expired = (2000 + int(yy), int(mm)) < (today.year, today.month)
    return ToolResult(True, {"name": c.name, "tier": c.tier, "plan": c.plan, "billing_cycle": c.billing_cycle,
                             "account_status": c.account_status, "card_last4": c.card_last4, "card_expiry": c.card_expiry,
                             "card_expired": expired, "member_since": day(c.created_at)}, source="db:customer")


# ------------------------------------------------------------------ billing
class GetInvoicesArgs(StrictArgs):
    limit: int = Field(5, ge=1, le=20, description="How many recent invoices to return")
    status: Literal["paid", "open", "failed", "refunded", "void"] | None = None


async def h_get_invoices(ctx: ToolContext, a: GetInvoicesArgs) -> ToolResult:
    q = (select(m.Invoice, m.Payment).join(m.Payment, m.Payment.invoice_id == m.Invoice.id, isouter=True)
         .where(m.Invoice.customer_id == ctx.customer_id))
    if a.status:
        q = q.where(m.Invoice.status == a.status)
    q = q.order_by(m.Invoice.issued_at.desc(), m.Invoice.id.desc()).limit(a.limit)
    async with session_scope() as s:
        rows = (await s.execute(q)).all()
    out = [{"invoice_id": i.id, "amount": usd(i.amount_cents), "description": i.description, "period": i.period,
            "issued": day(i.issued_at), "status": i.status,
            "payment_status": p.status if p else None, "failure_reason": p.failure_reason if p else None}
           for i, p in rows]
    return ToolResult(True, {"invoices": out, "count": len(out)}, source="db:invoices")


class InvoiceArgs(StrictArgs):
    invoice_id: InvoiceId = Field(description="Invoice id like INV-00001234")


async def h_payment_status(ctx: ToolContext, a: InvoiceArgs) -> ToolResult:
    async with session_scope() as s:
        inv = (await s.execute(select(m.Invoice).where(m.Invoice.id == a.invoice_id, m.Invoice.customer_id == ctx.customer_id))).scalar_one_or_none()
        if not inv:
            return ToolResult(False, error=NOT_FOUND)
        pays = (await s.execute(select(m.Payment).where(m.Payment.invoice_id == inv.id, m.Payment.customer_id == ctx.customer_id)
                                .order_by(m.Payment.created_at))).scalars().all()
    return ToolResult(True, {"invoice_id": inv.id, "invoice_status": inv.status, "amount": usd(inv.amount_cents),
                             "payments": [{"payment_id": p.id, "status": p.status, "amount": usd(p.amount_cents),
                                           "failure_reason": p.failure_reason, "date": day(p.created_at)} for p in pays]},
                      source=f"db:payment:{inv.id}")


async def _eligibility(ctx: ToolContext, invoice_id: str):
    async with session_scope() as s:
        inv = (await s.execute(select(m.Invoice).where(m.Invoice.id == invoice_id, m.Invoice.customer_id == ctx.customer_id))).scalar_one_or_none()
        if not inv:
            return None, None, None
        pay = (await s.execute(select(m.Payment).where(m.Payment.invoice_id == inv.id, m.Payment.status.in_(("succeeded", "refunded")))
                               .order_by(m.Payment.created_at.desc()))).scalars().first()
        sib = (await s.execute(select(m.Invoice).where(m.Invoice.customer_id == ctx.customer_id, m.Invoice.period == inv.period))).scalars().all()
        existing = (await s.execute(select(m.RefundRequest).where(m.RefundRequest.invoice_id == inv.id,
                                                                  m.RefundRequest.customer_id == ctx.customer_id))).scalars().first()
    dec = rules.refund_decision(inv, pay, sib, await business_today(), int(get_settings().refund_auto_limit_usd * 100))
    return inv, dec, existing


async def h_refund_eligibility(ctx: ToolContext, a: InvoiceArgs) -> ToolResult:
    inv, dec, existing = await _eligibility(ctx, a.invoice_id)
    if inv is None:
        return ToolResult(False, error=NOT_FOUND)
    d = dec.as_dict()
    d.update(invoice_id=inv.id, invoice_amount=usd(inv.amount_cents),
             existing_refund_request=None if not existing else {"refund_id": existing.id, "status": existing.status, "amount": usd(existing.amount_cents)})
    if dec.requires_approval:
        d["note"] = f"Refund exceeds ${get_settings().refund_auto_limit_usd:,.0f}: needs approval by a human billing specialist."
    return ToolResult(True, d, source=f"db:refund_eligibility:{inv.id}")


class RefundArgs(StrictArgs):
    invoice_id: InvoiceId
    reason: str = Field(min_length=5, max_length=300, description="Why the customer is requesting the refund")


async def h_create_refund(ctx: ToolContext, a: RefundArgs) -> ToolResult:
    inv, dec, existing = await _eligibility(ctx, a.invoice_id)   # G-TOOL-06: re-derive; never trust the model's claim of eligibility
    if inv is None:
        return ToolResult(False, error=NOT_FOUND)
    if existing:  # G-TOOL-09 idempotent
        return ToolResult(True, {"refund_id": existing.id, "status": existing.status, "amount": usd(existing.amount_cents),
                                 "note": "a refund request already exists for this invoice"},
                          source=f"db:refund:{existing.id}", requires_human=existing.status == "pending_approval")
    if not dec.eligible:
        return ToolResult(False, error=f"not eligible for refund: {dec.explanation}")
    status = "pending_approval" if dec.requires_approval else "approved"
    async with session_scope() as s:
        rid = await next_id(s, "REF", 8)
        rr = m.RefundRequest(id=rid, invoice_id=inv.id, customer_id=ctx.customer_id, amount_cents=dec.refundable_cents,
                             reason=a.reason, status=status, created_by=f"agent:{ctx.agent}", created_at=utcnow())
        s.add(rr)
    return ToolResult(True, {"refund_id": rr.id, "status": status, "amount": usd(rr.amount_cents), "invoice_id": inv.id,
                             "next_step": ("A human billing specialist must approve this refund (amount over the auto-approval limit); "
                                           "usually within 1 business day." if status == "pending_approval" else
                                           "Refund approved; it reaches the original card in 5-10 business days."),
                             "reason_code": dec.reason_code}, source=f"db:refund:{rr.id}", requires_human=status == "pending_approval")


# ------------------------------------------------------------------ technical
async def h_service_status(ctx: ToolContext, a: NoArgs) -> ToolResult:
    async with session_scope() as s:
        rows = (await s.execute(select(m.ServiceComponent).order_by(m.ServiceComponent.name))).scalars().all()
    comps = [{"component": r.name, "status": r.status, "note": r.note} for r in rows]
    return ToolResult(True, {"components": comps, "all_operational": all(c["status"] == "operational" for c in comps)},
                      source="db:service_status")


class LogsArgs(StrictArgs):
    window_hours: int = Field(24, ge=1, le=168)
    level: Literal["INFO", "WARN", "ERROR"] | None = None


async def h_user_logs(ctx: ToolContext, a: LogsArgs) -> ToolResult:
    today = await business_today()
    since = datetime.combine(today, datetime.min.time()) + timedelta(days=1) - timedelta(hours=a.window_hours)
    q = (select(m.ErrorLog.code, func.count(), func.max(m.ErrorLog.ts), func.max(m.ErrorLog.message), func.max(m.ErrorLog.level))
         .where(m.ErrorLog.customer_id == ctx.customer_id, m.ErrorLog.ts >= since))
    if a.level:
        q = q.where(m.ErrorLog.level == a.level)
    q = q.group_by(m.ErrorLog.code).order_by(func.count().desc())
    async with session_scope() as s:
        rows = (await s.execute(q)).all()
    by = [{"code": c, "count": n, "last_seen": ts.isoformat(timespec="minutes"), "message": msg, "level": lvl} for c, n, ts, msg, lvl in rows]
    return ToolResult(True, {"window_hours": a.window_hours, "total_events": sum(x["count"] for x in by), "by_code": by},
                      source="db:error_logs")


class TicketArgs(StrictArgs):
    summary: str = Field(min_length=10, max_length=300)
    severity: Literal["low", "medium", "high", "critical"] = "medium"
    category: Literal["billing", "technical", "account", "other"] = "technical"


async def h_create_ticket(ctx: ToolContext, a: TicketArgs) -> ToolResult:
    async with session_scope() as s:
        dup = (await s.execute(select(m.Ticket).where(m.Ticket.customer_id == ctx.customer_id, m.Ticket.summary == a.summary,
                                                      m.Ticket.created_at >= utcnow() - timedelta(minutes=10)))).scalars().first()
        if dup:  # G-TOOL-09
            return ToolResult(True, {"ticket_id": dup.id, "status": dup.status, "note": "existing ticket reused"}, source=f"db:ticket:{dup.id}")
        tid = await next_id(s, "TCK", 6)
        t = m.Ticket(id=tid, customer_id=ctx.customer_id, summary=a.summary, category=a.category,
                     severity=a.severity, status="open", created_at=utcnow())
        s.add(t)
    return ToolResult(True, {"ticket_id": t.id, "status": "open", "severity": a.severity}, source=f"db:ticket:{t.id}")


class DiagArgs(StrictArgs):
    check: Literal["api_key_status", "rate_limit_usage", "webhook_endpoint", "sso_config", "account_status"]


_DIAG_CODE = {"api_key_status": "AUTH_401_INVALID_TOKEN", "rate_limit_usage": "RATE_LIMIT_429",
              "webhook_endpoint": "WEBHOOK_TIMEOUT", "sso_config": "SSO_SAML_ASSERTION_EXPIRED"}


async def h_diagnostic(ctx: ToolContext, a: DiagArgs) -> ToolResult:
    """Simulated, read-only diagnostics derived from the customer's own logs/profile (deterministic)."""
    async with session_scope() as s:
        c = await s.get(m.Customer, ctx.customer_id)
        if a.check == "account_status":
            return ToolResult(True, {"check": a.check, "result": c.account_status, "plan": c.plan,
                                     "healthy": c.account_status == "active"}, source="diag:account_status")
        n = (await s.execute(select(func.count()).select_from(m.ErrorLog).where(
            m.ErrorLog.customer_id == ctx.customer_id, m.ErrorLog.code == _DIAG_CODE[a.check]))).scalar_one()
    detail = {"api_key_status": "API key rejected" if n else "No key rejections recorded",
              "rate_limit_usage": "Plan rate limit exceeded repeatedly" if n else "Within rate limit",
              "webhook_endpoint": "Endpoint timing out (>10s)" if n else "Endpoint responding normally",
              "sso_config": "SAML assertion rejected (clock skew)" if n else "No SSO errors recorded"}[a.check]
    return ToolResult(True, {"check": a.check, "healthy": n == 0, "recent_failures": n, "finding": detail,
                             "plan": c.plan}, source=f"diag:{a.check}")


# ------------------------------------------------------------------ escalation
class HistoryArgs(StrictArgs):
    limit: int = Field(5, ge=1, le=10)


async def h_ticket_history(ctx: ToolContext, a: HistoryArgs) -> ToolResult:
    async with session_scope() as s:
        rows = (await s.execute(select(m.Ticket).where(m.Ticket.customer_id == ctx.customer_id)
                                .order_by(m.Ticket.created_at.desc()).limit(a.limit))).scalars().all()
    return ToolResult(True, {"tickets": [{"ticket_id": t.id, "summary": t.summary, "category": t.category, "severity": t.severity,
                                          "status": t.status, "created": day(t.created_at)} for t in rows],
                             "open_or_escalated": sum(1 for t in rows if t.status in ("open", "escalated"))}, source="db:tickets")


ETA = {"standard": "within 1 business day", "premium": "within 4 hours", "vip": "within 1 hour"}


class AssignArgs(StrictArgs):
    queue: Literal["billing", "technical", "escalations", "security"]
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
            row = m.HumanReview(id=hid, query_id=ctx.query_id, customer_id=ctx.customer_id, priority=a.priority,
                                reason=a.reason, summary=a.summary, customer_message=a.customer_message, status="pending")
            s.add(row)
        else:  # G-TOOL-09: one queue row per query; later calls enrich it
            row.priority, row.reason, row.summary = a.priority, a.reason, a.summary
    eta = ETA.get(c.tier if c else "standard", ETA["standard"])
    return ToolResult(True, {"review_id": row.id, "queue": a.queue, "priority": a.priority, "eta": eta},
                      source=f"db:review:{row.id}", requires_human=True)


def register_all():
    A = {"billing", "technical", "escalation"}
    register(ToolSpec("search_knowledge_base", "Search the help-center knowledge base.",
                      KBArgs, h_search_kb, {"billing", "technical", "general"}))
    register(ToolSpec("get_customer_profile", "Customer plan, tier, status, card summary.",
                      NoArgs, h_profile, A | {"general"}))
    register(ToolSpec("get_invoices", "Recent invoices with payment status, newest first.",
                      GetInvoicesArgs, h_get_invoices, {"billing"}))
    register(ToolSpec("get_payment_status", "Payment attempts for one invoice.",
                      InvoiceArgs, h_payment_status, {"billing"}, strict_keys={"invoice_id"}))
    register(ToolSpec("check_refund_eligibility", "Refund eligibility for an invoice (policy engine).",
                      InvoiceArgs, h_refund_eligibility, {"billing"}, strict_keys={"invoice_id"}))
    register(ToolSpec("create_refund_request", "Create a refund request (only when the customer asked and eligible).",
                      RefundArgs, h_create_refund, {"billing"}, write=True, strict_keys={"invoice_id"}))
    register(ToolSpec("get_service_status", "Live status of Orbit components.",
                      NoArgs, h_service_status, {"technical"}))
    register(ToolSpec("get_user_logs", "Recent error events by code.",
                      LogsArgs, h_user_logs, {"technical"}))
    register(ToolSpec("create_ticket", "Open a support ticket (only when the customer asks for one).",
                      TicketArgs, h_create_ticket, {"technical", "billing"}, write=True))
    register(ToolSpec("run_diagnostic", "Run a read-only account diagnostic.",
                      DiagArgs, h_diagnostic, {"technical"}, strict_keys={"check"}))
    register(ToolSpec("get_ticket_history", "Get the customer's recent support tickets.",
                      HistoryArgs, h_ticket_history, {"escalation"}))
    register(ToolSpec("assign_to_human", "Hand the case to a human specialist queue.",
                      AssignArgs, h_assign_to_human, {"escalation"}, write=True))


register_all()
