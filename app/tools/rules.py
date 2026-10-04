"""Deterministic business rules (NO LLM). The refund rules engine is the single source of truth:
the Billing agent calls it through a tool, the validator re-derives amounts from it, and the KB
refund policy page (kb/billing-refund-policy.md) must stay in sync with these constants.
Guardrail id: G-TOOL-06 (policy decisions are code, not model opinion)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

MONTHLY_WINDOW_DAYS = 30
ANNUAL_WINDOW_DAYS = 14
DUPLICATE_WINDOW_DAYS = 90
DUPLICATE_GAP_HOURS = 48
PLAN_PRICES_CENTS = {"free": 0, "starter": 1900, "pro": 4900, "business": 14900, "enterprise": 49900}


@dataclass
class RefundDecision:
    eligible: bool
    reason_code: str  # within_window | duplicate_charge | outside_window | already_refunded | no_successful_payment
    explanation: str
    refundable_cents: int
    requires_approval: bool
    days_since_payment: int | None
    window_days: int | None
    policy_ref: str = "billing-refund-policy"

    def as_dict(self) -> dict:
        d = self.__dict__.copy()
        d["refundable_usd"] = f"${self.refundable_cents / 100:,.2f}"
        return d


def is_duplicate(invoice, siblings) -> bool:
    """True if `invoice` is the LATER of two paid invoices with same amount+period within 48h."""
    for o in siblings:
        if o.id == invoice.id or o.status not in ("paid", "refunded"):
            continue
        if o.amount_cents == invoice.amount_cents and o.period == invoice.period:
            gap = abs((invoice.issued_at - o.issued_at).total_seconds()) / 3600
            if gap <= DUPLICATE_GAP_HOURS and (invoice.issued_at, invoice.id) > (o.issued_at, o.id):
                return True
    return False


def refund_decision(invoice, payment, siblings, today: date, auto_limit_cents: int) -> RefundDecision:
    if invoice.status == "refunded":
        return RefundDecision(False, "already_refunded", "This invoice was already refunded.", 0, False, None, None)
    if invoice.status != "paid" or payment is None or payment.status != "succeeded":
        return RefundDecision(False, "no_successful_payment",
                              "There is no successful payment on this invoice, so there is nothing to refund.",
                              0, False, None, None)
    days = (today - payment.created_at.date()).days
    annual = "annual" in invoice.description.lower()
    window = ANNUAL_WINDOW_DAYS if annual else MONTHLY_WINDOW_DAYS
    amount = invoice.amount_cents
    needs = amount > auto_limit_cents
    if is_duplicate(invoice, siblings) and days <= DUPLICATE_WINDOW_DAYS:
        return RefundDecision(True, "duplicate_charge",
                              f"Duplicate charge detected (same amount and period within {DUPLICATE_GAP_HOURS}h); "
                              f"duplicates are refundable within {DUPLICATE_WINDOW_DAYS} days.",
                              amount, needs, days, DUPLICATE_WINDOW_DAYS)
    if days <= window:
        return RefundDecision(True, "within_window",
                              f"Paid {days} day(s) ago, inside the {window}-day refund window.",
                              amount, needs, days, window)
    return RefundDecision(False, "outside_window",
                          f"Paid {days} days ago, outside the {window}-day refund window for "
                          f"{'annual' if annual else 'monthly'} plans.", 0, False, days, window)
