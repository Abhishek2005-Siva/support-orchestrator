"""The investigator: VERIFY before ACT.

`derive()` is a pure-ish function of the database: it asks the knowledge graph which checks and policies apply to an issue type,
runs those checks against the customer's own rows with parameters from the `policies` table, and returns a decision. `verify()` wraps it,
stores a Verification row (the audit record of what was consulted) and returns a customer-safe report. Action tools call `derive()` again
so a model can never act on a stale or invented verification (G-TOOL-13).

Decisions:  act      -> an action is allowed (see `action`; `approval` says whether a human must approve it first)
            wait     -> nothing to do yet; explain the expected timing
            deny     -> policy forbids the action; explain why
            no_action-> informational (e.g. a decline reason, a completed transfer)
            human    -> hand over (conflicting or sensitive: never auto-resolved)
Internal facts (risk flags, fraud scores) influence the decision but are never included in the customer-visible output."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select

from app.db import models as m
from app.db.ids import next_id
from app.db.session import session_scope
from app.tools import kg, rules
from app.tools.rules import Check, PolicyBook, usd

DB_TABLES = {"duplicate_charge": ["customers", "accounts", "cards", "transactions", "disputes"],
             "unrecognised_payment": ["customers", "accounts", "cards", "transactions", "merchants", "fraud_alerts", "disputes"],
             "lost_stolen_card": ["cards"], "transfer_trace": ["customers", "transfers", "transactions"], "transfer_cancel": ["transfers"],
             "fee_dispute": ["customers", "accounts", "transactions", "fee_waivers"], "declined_payment": ["transactions", "cards", "merchants"]}


@dataclass
class Derived:
    issue_type: str
    subject_id: str
    decision: str
    reason_code: str
    explanation: str
    action: str | None = None
    actions: list[str] = field(default_factory=list)
    approval: str | None = None  # auto | required
    amount_cents: int = 0
    checks: list[Check] = field(default_factory=list)
    plan: dict = field(default_factory=dict)
    book: PolicyBook | None = None
    facts: dict = field(default_factory=dict)
    internal: bool = False  # an internal flag shaped the decision; never shown


async def load_book() -> PolicyBook:
    async with session_scope() as s:
        rows = (await s.execute(select(m.Policy))).scalars().all()
    return PolicyBook.from_rows(rows) if rows else PolicyBook.default()


class _Ctx:
    """Everything the checks may read, loaded once, always scoped to the authenticated customer."""
    pass


async def _load(customer_id: str, today) -> _Ctx:
    x = _Ctx()
    async with session_scope() as s:
        x.customer = await s.get(m.Customer, customer_id)
        x.accounts = {a.id: a for a in (await s.execute(select(m.Account).where(m.Account.customer_id == customer_id))).scalars().all()}
        x.cards = {c.id: c for c in (await s.execute(select(m.Card).where(m.Card.customer_id == customer_id))).scalars().all()}
        x.txns = (await s.execute(select(m.Transaction).where(m.Transaction.customer_id == customer_id).order_by(m.Transaction.created_at.desc()).limit(600))).scalars().all()
        x.disputes = (await s.execute(select(m.Dispute).where(m.Dispute.customer_id == customer_id))).scalars().all()
        x.alerts = (await s.execute(select(m.FraudAlert).where(m.FraudAlert.customer_id == customer_id))).scalars().all()
        x.waivers = (await s.execute(select(m.FeeWaiver).where(m.FeeWaiver.customer_id == customer_id))).scalars().all()
        x.merchants = {mm.id: mm for mm in (await s.execute(select(m.Merchant))).scalars().all()}
        x.transfers = (await s.execute(select(m.Transfer).where(m.Transfer.customer_id == customer_id))).scalars().all()
    x.today = today
    return x


def _txn(x, txn_id):
    return next((t for t in x.txns if t.id == txn_id), None)


def _run_checks(plan: dict, x, book: PolicyBook, subject) -> list[Check]:
    """Execute the checks the knowledge graph asked for, in order. A check the graph names but the code lacks is reported, not skipped."""
    out: list[Check] = []
    ctx: dict = {"group": None, "target": subject if hasattr(subject, "kind") else None}
    account = x.accounts.get(getattr(subject, "account_id", None)) if subject is not None else None
    card = x.cards.get(getattr(subject, "card_id", None)) if subject is not None and hasattr(subject, "card_id") else (subject if isinstance(subject, m.Card) else None)
    for name in plan["checks"]:
        if name == "same_card_merchant_amount":
            c = rules.check_same_card_merchant_amount(subject, x.txns, book); ctx["group"] = c.data.get("group"); out.append(c)
        elif name == "pending_vs_posted":
            if ctx["group"]:
                c = rules.check_pending_vs_posted(ctx["group"], x.today, book); ctx["kind"] = c.data.get("kind"); out.append(c)
        elif name == "already_disputed_or_credited":
            tgt = ctx["target"] or subject
            if ctx["group"]:
                posted = [t for t in ctx["group"] if t.status == "posted"]
                tgt = posted[-1] if posted else ctx["group"][-1]
                ctx["target"] = tgt
            out.append(rules.check_already_disputed_or_credited(tgt, x.disputes, x.txns))
        elif name == "dispute_window":
            tgt = ctx["target"] or subject
            out.append(rules.check_dispute_window(tgt, x.today, book))
        elif name == "provisional_credit_eligibility":
            tgt = ctx["target"] or subject
            out.append(rules.check_provisional_credit_eligibility(x.customer, x.accounts.get(tgt.account_id), tgt.amount_cents, x.today, book))
        elif name == "merchant_history":
            out.append(rules.check_merchant_history(subject, x.txns, x.today))
        elif name == "geo_mismatch":
            out.append(rules.check_geo_mismatch(subject, x.customer))
        elif name == "velocity":
            out.append(rules.check_velocity(subject, x.txns, book))
        elif name == "high_risk_merchant":
            out.append(rules.check_high_risk_merchant(subject, x.merchants.get(subject.merchant_id)))
        elif name == "open_fraud_alert":
            out.append(rules.check_open_fraud_alert(subject, x.alerts))
        elif name == "card_status":
            out.append(rules.check_card_status(card))
        elif name == "rail_timing":
            out.append(rules.check_rail_timing(subject, x.today, book))
        elif name == "return_code":
            out.append(rules.check_return_code(subject, x.txns))
        elif name == "cancellable":
            out.append(rules.check_cancellable(subject, book))
        elif name == "fee_type_limit":
            out.append(rules.check_fee_type_limit(subject, book))
        elif name == "waiver_history":
            out.append(rules.check_waiver_history(subject, x.waivers, x.today, book))
        elif name == "account_standing":
            out.append(rules.check_account_standing(account, subject, x.today, book))
        elif name == "decline_reason":
            out.append(rules.check_decline_reason(subject))
        elif name == "aml_flag":
            out.append(rules.check_aml_flag(x.customer))
        else:
            out.append(Check(name, "info", "No implementation for this check.", []))
    out.append(Check("_ctx", "info", "", [], {"target": ctx["target"], "group": ctx["group"], "kind": ctx.get("kind")}))
    return out


def _by(checks, name) -> Check | None:
    return next((c for c in checks if c.id == name), None)


def _is(checks, name, result) -> bool:
    c = _by(checks, name)
    return bool(c and c.result == result)


def _cr(c: Check | None, key: str, default=None):
    return c.data.get(key, default) if c else default


def _decide(issue: str, checks: list[Check], x, book: PolicyBook, subject) -> Derived:
    ctxc = _by(checks, "_ctx")
    target = ctxc.data.get("target") or subject
    internal = bool(_cr(_by(checks, "aml_flag"), "internal", False))
    subject_id = getattr(target, "id", None) or getattr(subject, "id", "")
    D = lambda dec, code, expl, **kw: Derived(issue, subject_id, dec, code, expl, **kw)  # noqa: E731

    if issue == "duplicate_charge":
        dup = _by(checks, "same_card_merchant_amount")
        if dup is None:
            return D("human", "missing_check", "The verification could not run its duplicate check.")
        if dup.result == "pass":
            return D("no_action", "no_duplicate", f"Only one purchase of {usd(subject.amount_cents)} on that card at that merchant in the {book.p('POL-DUP-01', 'window_hours')}-hour window, so this is not a duplicate. ({subject.id})")
        pv = _by(checks, "pending_vs_posted")
        kind = _cr(pv, "kind")
        if kind in ("hold_and_posted", "holds_only"):
            return D("wait", "pending_hold", pv.detail)
        if kind == "stale_hold":
            return D("human", "stale_hold", "A pending hold is older than the hold period and needs a specialist to release it.")
        if _is(checks, "already_disputed_or_credited", "flag"):
            return D("deny", "already_disputed", _by(checks, "already_disputed_or_credited").detail)
        if _is(checks, "dispute_window", "fail"):
            return D("deny", "outside_dispute_window", _by(checks, "dispute_window").detail)
        if internal:
            return D("human", "specialist_review", "This needs review by a specialist.", internal=True)
        auto = _is(checks, "provisional_credit_eligibility", "pass")   # a missing check never auto-approves
        return D("act", "duplicate_confirmed", f"Duplicate confirmed: {target.id} repeats an identical purchase of {usd(target.amount_cents)} on the same card at the same merchant. Eligible for a duplicate dispute.",
                 action="file_dispute", actions=["file_dispute"], approval="auto" if auto else "required", amount_cents=target.amount_cents)

    if issue == "unrecognised_payment":
        if _is(checks, "already_disputed_or_credited", "flag"):
            return D("deny", "already_disputed", _by(checks, "already_disputed_or_credited").detail)
        if _is(checks, "dispute_window", "fail"):
            return D("deny", "outside_dispute_window", _by(checks, "dispute_window").detail)
        if _cr(_by(checks, "merchant_history"), "recurring", False):
            return D("no_action", "known_merchant", _by(checks, "merchant_history").detail)
        if internal:
            return D("human", "specialist_review", "This needs review by a specialist.", internal=True)
        signals = [n for n in ("geo_mismatch", "velocity", "high_risk_merchant", "open_fraud_alert") if _by(checks, n) and _by(checks, n).result == "flag"]
        auto = _is(checks, "provisional_credit_eligibility", "pass")   # a missing check never auto-approves
        card_active = not _cr(_by(checks, "card_status"), "already_blocked", False) and _by(checks, "card_status") is not None and _is(checks, "card_status", "pass")
        if signals:
            acts = (["block_card"] if card_active else []) + ["file_dispute"] + (["request_replacement_card"] if card_active else [])
            return D("act", "fraud_signals", f"Fraud signals found ({', '.join(s.replace('_', ' ') for s in signals)}): block the card, dispute the payment as unauthorised and offer a replacement card.",
                     action="file_dispute", actions=acts, approval="auto" if auto else "required", amount_cents=target.amount_cents, facts={"block_card": card_active, "signals": signals})
        return D("act", "dispute_allowed", "No fraud signals and no earlier payments to this merchant. The payment can be disputed as unauthorised; the card does not need to be blocked.",
                 action="file_dispute", actions=["file_dispute"], approval="auto" if auto else "required", amount_cents=target.amount_cents, facts={"block_card": False, "signals": []})

    if issue == "lost_stolen_card":
        cs = _by(checks, "card_status")
        if _cr(cs, "already_blocked", False):
            return D("no_action", "already_blocked", cs.detail)
        return D("act", "block_allowed", "Card is active: blocking it now is allowed (zero liability for a card reported before misuse).", action="block_card", actions=["block_card", "request_replacement_card"], approval="auto")

    if issue == "transfer_trace":
        rt, rc = _by(checks, "rail_timing"), _by(checks, "return_code")
        tr = subject
        if rt is None:
            return D("human", "missing_check", "The verification could not run its timing check.")
        if tr.status == "completed":
            return D("no_action", "completed", f"{rt.detail} Beneficiary: {tr.to_name}, reference {tr.reference}.")
        if tr.status in ("returned", "failed"):
            if _cr(rc, "returned_ok", False):
                return D("no_action", "returned_to_account", rc.detail)
            return D("human", "return_not_credited", rc.detail if rc else "The return could not be checked.")
        if tr.status == "cancelled":
            return D("no_action", "cancelled", "The transfer was cancelled; no money was sent.")
        if internal and tr.rail == "wire":
            return D("human", "specialist_review", "This transfer needs a specialist to trace it.", internal=True)
        if _cr(rt, "overdue", False):
            return D("human", "overdue_trace", rt.detail + " A specialist must trace it.")
        return D("wait", "in_transit", rt.detail)

    if issue == "transfer_cancel":
        cc = _by(checks, "cancellable")
        if cc is None:
            return D("human", "missing_check", "The verification could not run its cancellation check.")
        if cc.result == "pass":
            return D("act", "cancel_allowed", cc.detail, action="cancel_transfer", actions=["cancel_transfer"], approval="auto", amount_cents=subject.amount_cents)
        return D("deny", "not_cancellable", cc.detail)

    if issue == "fee_dispute":
        ft = _by(checks, "fee_type_limit")
        if ft is None:
            return D("human", "missing_check", "The verification could not run its fee check.")
        if ft.result == "fail":
            return D("deny", "not_a_fee", ft.detail)
        if _is(checks, "already_disputed_or_credited", "flag"):
            return D("deny", "already_credited", _by(checks, "already_disputed_or_credited").detail)
        if _is(checks, "account_standing", "fail"):
            return D("deny", "not_in_good_standing", _by(checks, "account_standing").detail)
        if _is(checks, "waiver_history", "fail"):
            return D("deny", "waiver_used", _by(checks, "waiver_history").detail)
        if internal:
            return D("human", "specialist_review", "This needs review by a specialist.", internal=True)
        if ft.result == "flag":
            return D("human", "over_limit", ft.detail + " A specialist must review it.")
        return D("act", "waiver_allowed", f"{usd(subject.amount_cents)} fee is within the courtesy-waiver limit and no waiver was used in the last {book.p('POL-FEE-01', 'per_months')} months.",
                 action="reverse_fee", actions=["reverse_fee"], approval="auto", amount_cents=subject.amount_cents)

    if issue == "declined_payment":
        dr = _by(checks, "decline_reason")
        if dr is None:
            return D("human", "missing_check", "The verification could not run its decline check.")
        if not _cr(dr, "declined", False):
            return D("no_action", "not_declined", dr.detail)
        return D("no_action", "declined", dr.detail, facts={"decline_reason": _cr(dr, "reason")})
    return D("human", "unknown_issue", "Unknown issue type.")


async def _resolve_subject(x, issue: str, subject_id: str | None):
    if issue in ("transfer_trace", "transfer_cancel"):
        tr = next((t for t in x.transfers if t.id == subject_id), None) if subject_id else (sorted(x.transfers, key=lambda t: t.initiated_at)[-1] if x.transfers else None)
        return tr
    if issue == "lost_stolen_card":
        if subject_id:
            return x.cards.get(subject_id)
        active = [c for c in x.cards.values() if c.status == "active"]
        return active[0] if len(active) == 1 else None
    return _txn(x, subject_id) if subject_id else None


async def derive(customer_id: str, issue_type: str, subject_id: str | None, today) -> tuple[Derived | None, str | None, _Ctx | None]:
    """(derived, error, context). Errors are customer-safe and recoverable (the model can retry with a valid id)."""
    if issue_type not in kg.ISSUE_TYPES:
        return None, f"unknown issue_type; choose one of {list(kg.ISSUE_TYPES)}", None
    plan = await kg.plan(issue_type)
    if not plan["checks"]:
        return None, "the knowledge graph has no checks for this issue type", None
    book = await load_book()
    x = await _load(customer_id, today)
    if x.customer is None:
        return None, "customer not found", None
    subject = await _resolve_subject(x, issue_type, subject_id)
    if subject is None:
        kind = {"transfer_trace": "transfer", "transfer_cancel": "transfer", "lost_stolen_card": "card"}.get(issue_type, "transaction")
        return None, f"no such {kind} for this customer" + ("; pass the id from get_transactions / get_transfer_status / get_cards" if subject_id is None or True else ""), None
    if issue_type in ("duplicate_charge", "unrecognised_payment") and subject.kind not in ("card_purchase", "atm_withdrawal"):
        return None, f"{subject.id} is a {subject.kind}, not a card payment", None
    if issue_type == "fee_dispute" and subject.kind != "fee":
        return None, f"{subject.id} is not a fee (it is a {subject.kind})", None
    checks = _run_checks(plan, x, book, subject)
    d = _decide(issue_type, checks, x, book, subject)
    d.checks, d.plan, d.book = checks, plan, book
    return d, None, x


def public_report(d: Derived, verification_id: str | None) -> dict:
    """Customer-safe view: no internal flags, scores or the aml check."""
    checks = [c.as_dict() for c in d.checks if c.id not in ("_ctx", "aml_flag")]
    pub = d.plan.get("public", {})
    pols = [d.book.cite(p) for p in pub.get("policies", [])] if d.book else []
    out = {"verification_id": verification_id, "issue_type": d.issue_type, "subject_id": d.subject_id, "decision": d.decision, "reason_code": d.reason_code,
           "explanation": d.explanation, "checks": checks,
           "consulted": {"database_tables": DB_TABLES.get(d.issue_type, []), "policies": [p["policy"] for p in pols], "regulations": pub.get("regulations", []),
                         "knowledge_graph_paths": pub.get("paths", [])[:12], "knowledge_base": pub.get("kb", [])}}
    out["policy_values"] = [{"policy": p["policy"], "title": p["title"], "params": p["params"]} for p in pols]
    if d.decision == "act":
        out.update(allowed_actions=d.actions, primary_action=d.action, approval=d.approval, amount=usd(d.amount_cents) if d.amount_cents else None)
        if d.approval == "required":
            out["approval_note"] = "A human specialist must approve before any money moves; the customer must be told it is pending."
        out.update({k: v for k, v in d.facts.items() if k in ("block_card", "signals")})
    if d.decision == "human":
        out["next_step"] = "Hand this case to a human specialist (set needs_human=true). Do not mention internal reviews."
    return out


async def verify(customer_id: str, query_id: str | None, issue_type: str, subject_id: str | None, today) -> tuple[dict | None, str | None]:
    d, err, x = await derive(customer_id, issue_type, subject_id, today)
    if err:
        return None, err
    async with session_scope() as s:
        vid = await next_id(s, "VER", 6)
        full = public_report(d, vid)
        full["internal"] = {"internal_flag_used": d.internal, "internal_checks": [c.as_dict() for c in d.checks if c.id == "aml_flag"]}
        s.add(m.Verification(id=vid, customer_id=customer_id, query_id=query_id, issue_type=issue_type, subject_id=d.subject_id, decision=d.decision, action=d.action,
                             amount_cents=d.amount_cents, report=json.dumps(full, default=str)))
    return public_report(d, vid), None


async def require(customer_id: str, verification_id: str, action: str, today, max_age_min: int = 30) -> tuple[Derived | None, m.Verification | None, str | None]:
    """Gate for action tools: the verification must exist, belong to this customer, be recent, allow `action`, and the decision must still hold."""
    from app.db.models import utcnow
    from datetime import timedelta
    async with session_scope() as s:
        v = await s.get(m.Verification, verification_id)
    if v is None or v.customer_id != customer_id:
        return None, None, "no such verification for this customer; call verify_transaction_issue first"
    if utcnow() - v.created_at > timedelta(minutes=max_age_min):
        return None, v, "that verification is too old; call verify_transaction_issue again"
    d, err, _ = await derive(customer_id, v.issue_type, v.subject_id, today)
    if err:
        return None, v, err
    if d.decision != "act" or action not in d.actions:
        return None, v, f"verification does not allow '{action}' (decision: {d.decision}, reason: {d.reason_code})"
    return d, v, None
