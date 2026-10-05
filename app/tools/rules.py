"""Deterministic bank rules (NO LLM). Every number comes from the `policies` table (see DEFAULT_POLICIES, which is what the
seed writes), so changing a policy row changes behaviour and the verification report cites the row it used.
The checks here are pure functions over rows; `tools/verify.py` decides WHICH checks run by asking the knowledge graph.
Guardrail id: G-TOOL-06 (policy decisions are code, not model opinion)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------- policy table (seeded; also the defaults)
DEFAULT_POLICIES: list[dict] = [
    {"id": "POL-DUP-01", "title": "Duplicate charge detection", "regulation": "Card network rules", "kb": "duplicate-charge",
     "rule": "Two purchases on the same card at the same merchant for the same amount within the window are treated as a possible duplicate.",
     "params": {"window_hours": 24}},
    {"id": "POL-HOLD-01", "title": "Pending authorisation holds", "regulation": "Card network rules", "kb": "pending-payments",
     "rule": "An authorisation hold that has not yet posted drops off by itself within the hold period. A hold plus a posted charge for the same purchase is one charge, not two.",
     "params": {"hold_business_days": 5}},
    {"id": "POL-DSP-01", "title": "Dispute window", "regulation": "Reg E (EFTA) §1005.11", "kb": "dispute-a-transaction",
     "rule": "A customer can dispute a transaction within the window counted from the transaction date.",
     "params": {"window_days": 60, "investigation_business_days": 10}},
    {"id": "POL-DSP-02", "title": "Provisional credit (auto-approval)", "regulation": "Reg E §1005.11, bank policy", "kb": "provisional-credit",
     "rule": "Provisional credit is issued immediately up to the limit when the customer is KYC-verified, has no internal review flag and the account is older than the minimum age.",
     "params": {"auto_limit_usd": 500, "min_account_age_days": 30}},
    {"id": "POL-DSP-03", "title": "Provisional credit above the limit", "regulation": "Bank policy", "kb": "provisional-credit",
     "rule": "Disputes above the auto-approval limit, or by customers who do not meet its conditions, need a human specialist's approval.",
     "params": {}},
    {"id": "POL-FRD-01", "title": "Fraud signals", "regulation": "Bank fraud policy", "kb": "report-fraud",
     "rule": "Block the card and dispute as unauthorised when the payment shows a fraud signal: card used in a foreign country, 3 or more transactions on one card within an hour, a high-risk merchant category online, or an open fraud-engine alert.",
     "params": {"velocity_count": 3, "velocity_minutes": 60}},
    {"id": "POL-LIA-01", "title": "Liability for lost or stolen cards", "regulation": "Reg E §1005.6", "kb": "lost-or-stolen-card",
     "rule": "A customer who reports a lost or stolen card before it is used has zero liability. Blocking the card is always allowed on request.",
     "params": {"zero_liability_usd": 0}},
    {"id": "POL-FEE-01", "title": "Fee courtesy waiver", "regulation": "Bank policy, UDAAP", "kb": "fees-and-waivers",
     "rule": "One fee can be waived per customer in any 12 months, up to the limit, when the account is in good standing.",
     "params": {"limit_usd": 35, "per_months": 12, "window_days": 60}},
    {"id": "POL-ACH-01", "title": "ACH transfer timing", "regulation": "NACHA operating rules", "kb": "transfer-times",
     "rule": "ACH transfers arrive within the stated number of business days.",
     "params": {"business_days": 3}},
    {"id": "POL-WIR-01", "title": "Wire cut-off and finality", "regulation": "Fedwire", "kb": "wire-transfers",
     "rule": "Wires sent before the cut-off arrive the same business day and cannot be cancelled once released.",
     "params": {"cutoff": "17:00 ET", "cancellable": False, "business_days": 1}},
    {"id": "POL-CAN-01", "title": "Transfer cancellation", "regulation": "Bank policy", "kb": "cancel-a-transfer",
     "rule": "A transfer can be cancelled only while it is pending and has not been submitted to the rail. Wires cannot be cancelled.",
     "params": {"cancellable_status": ["pending"], "non_cancellable_rails": ["wire"]}},
    {"id": "POL-LIM-01", "title": "Daily limits", "regulation": "Bank policy", "kb": "limits",
     "rule": "Standard daily limits for card spend, ATM cash and wires.",
     "params": {"card_spend_usd": 2500, "atm_usd": 800, "wire_usd": 25000}},
    {"id": "POL-KYC-01", "title": "Review triggers", "regulation": "BSA / AML", "kb": "identity-checks",
     "rule": "Cash or wire activity of 10,000 USD or more, or patterns that look like structuring, are reviewed by compliance.",
     "params": {"threshold_usd": 10000}},
    {"id": "POL-AML-01", "title": "Suspected money laundering", "regulation": "BSA / AML", "kb": "identity-checks",
     "rule": "Escalate to compliance. Never tell the customer that a review exists (no tipping off).",
     "params": {}},
]

ACH_RETURN_CODES = {"R01": "the receiving account had insufficient funds", "R02": "the receiving account is closed",
                    "R03": "no account was found with those details", "R04": "the account number was invalid",
                    "R10": "the receiver said the debit was not authorised"}
DECLINE_FIX = {"insufficient_funds": "the account balance (plus any overdraft) was lower than the payment",
               "daily_limit": "the payment would have gone over the card's daily limit",
               "card_blocked": "the card is blocked",
               "intl_disabled": "the card is not enabled for payments abroad",
               "suspected_fraud": "the payment was paused by fraud monitoring",
               "expired_card": "the card has expired",
               "wrong_pin": "the PIN was entered incorrectly"}


@dataclass
class PolicyBook:
    rows: dict[str, dict] = field(default_factory=dict)  # id -> {title, rule, params, regulation, kb_article, version}

    @classmethod
    def default(cls) -> "PolicyBook":
        return cls({p["id"]: {"title": p["title"], "rule": p["rule"], "params": dict(p["params"]), "regulation": p["regulation"],
                              "kb_article": p["kb"], "version": "1.0"} for p in DEFAULT_POLICIES})

    def p(self, pid: str, key: str):
        return self.rows[pid]["params"][key]

    def cite(self, pid: str) -> dict:
        r = self.rows[pid]
        return {"policy": pid, "title": r["title"], "regulation": r["regulation"], "kb_article": r["kb_article"], "params": r["params"]}

    @classmethod
    def from_rows(cls, rows) -> "PolicyBook":
        return cls({r.id: {"title": r.title, "rule": r.rule, "params": json.loads(r.params), "regulation": r.regulation,
                           "kb_article": r.kb_article, "version": r.version} for r in rows})


# ---------------------------------------------------------------- helpers
def usd(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def add_business_days(d: date, n: int) -> date:
    while n > 0:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n -= 1
    return d


def business_days_between(a: date, b: date) -> int:
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


@dataclass
class Check:
    id: str
    result: str  # pass | flag | info | fail
    detail: str
    evidence: list[str] = field(default_factory=list)
    data: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {"check": self.id, "result": self.result, "detail": self.detail, "evidence": self.evidence}


# ---------------------------------------------------------------- duplicate charge
def duplicate_candidates(txn, others, book: PolicyBook) -> list:
    """Other card purchases on the same card, merchant and amount within the window (either side of `txn`)."""
    win = timedelta(hours=book.p("POL-DUP-01", "window_hours"))
    out = []
    for o in others:
        if o.id == txn.id or o.kind != "card_purchase" or o.direction != "debit" or o.status == "reversed":
            continue
        if o.card_id == txn.card_id and o.merchant_id == txn.merchant_id and o.amount_cents == txn.amount_cents \
                and abs(o.created_at - txn.created_at) <= win:
            out.append(o)
    return sorted(out, key=lambda t: (t.created_at, t.id))


def check_same_card_merchant_amount(txn, others, book) -> Check:
    dups = duplicate_candidates(txn, others, book)
    if not dups:
        return Check("same_card_merchant_amount", "pass", "No other purchase on the same card, at the same merchant, for the same amount in the window.", [txn.id])
    ids = [txn.id] + [d.id for d in dups]
    return Check("same_card_merchant_amount", "flag", f"{len(dups) + 1} purchases of {usd(txn.amount_cents)} on the same card at the same merchant within {book.p('POL-DUP-01', 'window_hours')} hours: {', '.join(sorted(ids))}.", ids,
                 {"group": sorted([txn] + dups, key=lambda t: (t.created_at, t.id))})


def check_pending_vs_posted(group, today: date, book) -> Check:
    pend = [t for t in group if t.status == "pending"]
    posted = [t for t in group if t.status == "posted"]
    hold_days = book.p("POL-HOLD-01", "hold_business_days")
    if pend and posted:
        return Check("pending_vs_posted", "info", f"One of the charges is still a pending authorisation hold ({pend[0].id}) and one has posted ({posted[0].id}): this is one purchase shown twice, and the hold falls away by itself within {hold_days} business days.", [pend[0].id, posted[0].id], {"kind": "hold_and_posted"})
    if pend and not posted:
        age = business_days_between(pend[0].created_at.date(), today)
        stale = age > hold_days
        return Check("pending_vs_posted", "flag" if stale else "info", f"All matching charges are pending holds; the oldest is {age} business day(s) old (hold period {hold_days}).", [t.id for t in pend], {"kind": "stale_hold" if stale else "holds_only"})
    return Check("pending_vs_posted", "pass", "Both charges have posted (settled); this is not a pending-hold display.", [t.id for t in posted], {"kind": "both_posted"})


def check_already_disputed_or_credited(txn, disputes, all_txns) -> Check:
    d = next((x for x in disputes if x.txn_id == txn.id and x.status != "rejected"), None)
    credit = next((t for t in all_txns if t.linked_txn_id == txn.id and t.kind in ("refund", "provisional_credit")), None)
    if d or credit or txn.status == "reversed":
        ev = [txn.id] + ([d.id] if d else []) + ([credit.id] if credit else [])
        why = "an open or closed dispute" if d else "a refund or credit" if credit else "a reversal"
        return Check("already_disputed_or_credited", "flag", f"{txn.id} already has {why} ({', '.join(ev[1:]) or 'status reversed'}).", ev, {"dispute": d, "credit": credit})
    return Check("already_disputed_or_credited", "pass", "No dispute, refund or credit exists for this transaction.", [txn.id])


def check_dispute_window(txn, today: date, book) -> Check:
    days = (today - txn.created_at.date()).days
    lim = book.p("POL-DSP-01", "window_days")
    if days > lim:
        return Check("dispute_window", "fail", f"The transaction is {days} days old; disputes must be raised within {lim} days (POL-DSP-01).", [txn.id], {"days": days})
    return Check("dispute_window", "pass", f"The transaction is {days} days old, inside the {lim}-day dispute window.", [txn.id], {"days": days})


def check_provisional_credit_eligibility(customer, account, amount_cents: int, today: date, book) -> Check:
    lim = int(book.p("POL-DSP-02", "auto_limit_usd") * 100)
    min_age = book.p("POL-DSP-02", "min_account_age_days")
    age = (today - account.opened_at.date()).days if account else 0
    reasons = []
    if customer.kyc_status != "verified":
        reasons.append("the customer is not yet identity-verified")
    if customer.risk_flag != "none":
        reasons.append("an internal review applies")  # never specific: no tipping off
    if age < min_age:
        reasons.append(f"the account is only {age} days old (minimum {min_age})")
    if amount_cents > lim:
        reasons.append(f"{usd(amount_cents)} is above the {usd(lim)} auto-approval limit")
    if reasons:
        return Check("provisional_credit_eligibility", "flag", "Provisional credit needs human approval: " + "; ".join(reasons) + ".", [account.id if account else ""],
                     {"auto": False, "internal": customer.risk_flag != "none"})
    return Check("provisional_credit_eligibility", "pass", f"{usd(amount_cents)} is within the {usd(lim)} auto-approval limit and the customer qualifies (POL-DSP-02).", [account.id], {"auto": True})


# ---------------------------------------------------------------- unrecognised payment
def check_merchant_history(txn, all_txns, today: date) -> Check:
    prior = [t for t in all_txns if t.id != txn.id and t.merchant_id and t.merchant_id == txn.merchant_id and t.status == "posted"
             and t.created_at < txn.created_at and (txn.created_at - t.created_at).days <= 120]
    if len(prior) >= 2:
        return Check("merchant_history", "info", f"The customer paid this merchant {len(prior)} times in the previous 120 days (for example on {', '.join(t.created_at.date().isoformat() for t in prior[:3])}): it is a merchant they use regularly.", [t.id for t in prior[:3]], {"recurring": True})
    return Check("merchant_history", "pass", "No earlier payments to this merchant in the previous 120 days.", [txn.id], {"recurring": False})


def check_geo_mismatch(txn, customer) -> Check:
    if txn.country != customer.country:
        return Check("geo_mismatch", "flag", f"The payment was made in {txn.country}; the customer's home country is {customer.country}.", [txn.id], {"foreign": True})
    return Check("geo_mismatch", "pass", "The payment was made in the customer's home country.", [txn.id], {"foreign": False})


def check_velocity(txn, all_txns, book) -> Check:
    n_lim, mins = book.p("POL-FRD-01", "velocity_count"), book.p("POL-FRD-01", "velocity_minutes")
    near = [t for t in all_txns if t.card_id and t.card_id == txn.card_id and abs((t.created_at - txn.created_at).total_seconds()) <= mins * 60]
    if len(near) >= n_lim:
        return Check("velocity", "flag", f"{len(near)} transactions on the same card within {mins} minutes ({', '.join(sorted(t.id for t in near)[:5])}).", [t.id for t in near][:6], {"count": len(near)})
    return Check("velocity", "pass", f"{len(near)} transaction(s) on the card within {mins} minutes; below the {n_lim} threshold.", [txn.id], {"count": len(near)})


def check_high_risk_merchant(txn, merchant) -> Check:
    if merchant and merchant.risk == "high" and txn.channel == "online":
        return Check("high_risk_merchant", "flag", f"Card-not-present payment at a high-risk merchant category ({merchant.category}).", [txn.id, merchant.id], {})
    return Check("high_risk_merchant", "pass", "Merchant category is not high risk for this channel.", [txn.id], {})


def check_open_fraud_alert(txn, alerts) -> Check:
    a = next((x for x in alerts if x.txn_id == txn.id and x.status == "open"), None)
    if a:
        return Check("open_fraud_alert", "flag", "The fraud-monitoring system had already flagged this transaction.", [txn.id, a.id], {})  # score never shown
    return Check("open_fraud_alert", "pass", "No open fraud-monitoring alert on this transaction.", [txn.id], {})


def check_card_status(card) -> Check:
    if card is None:
        return Check("card_status", "info", "The transaction was not made with a card.", [], {})
    if card.status != "active":
        return Check("card_status", "info", f"Card ending {card.last4} is already {card.status}.", [card.id], {"already_blocked": True})
    return Check("card_status", "pass", f"Card ending {card.last4} is active.", [card.id], {"already_blocked": False})


# ---------------------------------------------------------------- transfers
def check_rail_timing(tr, today: date, book) -> Check:
    rail = tr.rail
    days_allowed = book.p("POL-WIR-01", "business_days") if rail == "wire" else book.p("POL-ACH-01", "business_days") if rail in ("ach", "sepa") else 0
    elapsed = business_days_between(tr.initiated_at.date(), today)
    pol = "POL-WIR-01" if rail == "wire" else "POL-ACH-01"
    if tr.status == "completed":
        return Check("rail_timing", "pass", f"Completed on {tr.completed_at.date().isoformat() if tr.completed_at else 'n/a'}, {business_days_between(tr.initiated_at.date(), (tr.completed_at or tr.initiated_at).date())} business day(s) after it was sent.", [tr.id], {"overdue": False})
    if tr.status in ("pending", "submitted"):
        overdue = elapsed > days_allowed
        return Check("rail_timing", "flag" if overdue else "info", f"{rail.upper()} transfers arrive within {days_allowed} business day(s) ({pol}); {elapsed} business day(s) have passed" + (": overdue." if overdue else ": still within the normal time."), [tr.id], {"overdue": overdue, "elapsed": elapsed, "allowed": days_allowed})
    return Check("rail_timing", "info", f"Transfer status is {tr.status}.", [tr.id], {"overdue": False})


def check_return_code(tr, all_txns) -> Check:
    if tr.status not in ("returned", "failed"):
        return Check("return_code", "pass", "The transfer was not returned.", [tr.id], {})
    why = ACH_RETURN_CODES.get(tr.return_code or "", "the receiving bank could not accept it")
    credit = next((t for t in all_txns if t.linked_txn_id == tr.txn_id and t.direction == "credit" and t.status in ("posted", "pending")), None) if tr.txn_id else None
    returned_ok = tr.status == "failed" or credit is not None
    return Check("return_code", "info", f"The transfer was {tr.status}" + (f" (code {tr.return_code}: {why})" if tr.return_code else "") + (". The money was returned to the account." if returned_ok else ". No matching return credit was found in the ledger."),
                 [tr.id] + ([credit.id] if credit else []), {"returned_ok": returned_ok, "why": why})


def check_cancellable(tr, book) -> Check:
    if tr.rail in book.p("POL-CAN-01", "non_cancellable_rails"):
        return Check("cancellable", "fail", "Wires cannot be cancelled once released (POL-WIR-01 / POL-CAN-01).", [tr.id], {"cancellable": False})
    if tr.status not in book.p("POL-CAN-01", "cancellable_status"):
        return Check("cancellable", "fail", f"The transfer is {tr.status}; only pending transfers that have not been submitted can be cancelled (POL-CAN-01).", [tr.id], {"cancellable": False})
    return Check("cancellable", "pass", "The transfer is pending and has not been submitted, so it can be cancelled.", [tr.id], {"cancellable": True})


# ---------------------------------------------------------------- fees
def check_fee_type_limit(fee, book) -> Check:
    if fee.kind != "fee":
        return Check("fee_type_limit", "fail", f"{fee.id} is not a fee.", [fee.id], {})
    lim = int(book.p("POL-FEE-01", "limit_usd") * 100)
    if fee.amount_cents > lim:
        return Check("fee_type_limit", "flag", f"The fee ({usd(fee.amount_cents)}) is above the {usd(lim)} courtesy-waiver limit (POL-FEE-01).", [fee.id], {"over_limit": True})
    return Check("fee_type_limit", "pass", f"The fee ({usd(fee.amount_cents)}) is within the {usd(lim)} courtesy-waiver limit.", [fee.id], {"over_limit": False})


def check_waiver_history(fee, waivers, today: date, book) -> Check:
    months = book.p("POL-FEE-01", "per_months")
    recent = [w for w in waivers if (today - w.waived_at.date()).days <= months * 30]
    if recent:
        return Check("waiver_history", "fail", f"A fee was already waived on {recent[0].waived_at.date().isoformat()}; one courtesy waiver is allowed per {months} months (POL-FEE-01).", [recent[0].fee_txn_id], {"used": True})
    return Check("waiver_history", "pass", f"No fee waiver in the last {months} months.", [], {"used": False})


def check_account_standing(account, fee, today: date, book) -> Check:
    age = (today - fee.created_at.date()).days
    lim = book.p("POL-FEE-01", "window_days")
    bad = []
    if account.status != "active":
        bad.append(f"the account is {account.status}")
    if age > lim:
        bad.append(f"the fee is {age} days old (limit {lim})")
    if bad:
        return Check("account_standing", "fail", "Not eligible: " + "; ".join(bad) + ".", [account.id, fee.id], {})
    return Check("account_standing", "pass", "The account is active and the fee is recent.", [account.id, fee.id], {})


# ---------------------------------------------------------------- declines
def check_decline_reason(txn) -> Check:
    if txn.status != "declined":
        return Check("decline_reason", "info", f"{txn.id} was not declined (status {txn.status}).", [txn.id], {"declined": False})
    why = DECLINE_FIX.get(txn.decline_reason or "", "the payment could not be authorised")
    return Check("decline_reason", "flag", f"Declined because {why}.", [txn.id], {"declined": True, "reason": txn.decline_reason})


def check_aml_flag(customer) -> Check:
    if customer.risk_flag != "none":
        return Check("aml_flag", "flag", "An internal review applies to this customer.", [], {"internal": True})  # never reaches the customer
    return Check("aml_flag", "pass", "No internal review applies.", [], {})
