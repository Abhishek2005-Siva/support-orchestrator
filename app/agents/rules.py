"""Deterministic escalation triggers (no LLM). They OVERRIDE the model's routing so safety-critical cases can never be
mis-routed by a classification slip. Guardrail id: G-AGENT-02. Each trigger returns a stable reason code."""
from __future__ import annotations

import re

_P = lambda p: re.compile(p, re.I)
TRIGGERS: list[tuple[str, re.Pattern]] = [
    ("legal_threat", _P(r"\b(lawyer|attorney|solicitor|sue\b|suing|lawsuit|legal action|take legal|court|small claims|legal department|my legal team|regulator|ombudsman|consumer protection|ftc|bbb)\b")),
    ("data_breach_security", _P(r"\b(data breach|breach(ed)?|hacked|hack(ed)? my|account (was )?compromised|compromised account|unauthori[sz]ed (access|login|charge|transaction|activity)|someone (else )?(logged|accessed|used) (in|my)|stolen|leaked|leak(ed)? (my|our) data|phishing|identity theft|security incident|exposed (my|our) (data|keys?))\b")),
    ("fraud_or_chargeback", _P(r"\b(fraud(ulent)?|scam|chargeback|charge ?back|dispute (the|this|a) charge|disputing (the|this|a) charge|report(ing)? (you|this) to (my )?bank|stolen card)\b")),
    ("explicit_human_request", _P(r"\b(speak|speaking|talk|talking|chat|chatting|connect me|transfer me|escalate|get me|put me through|hand me over)\b.{0,30}\b(human|real person|person|agent|assistant|advisor|operator|employee|staff|team member|manager|supervisor|representative|someone|specialist)\b|\b(human|real person|manager|supervisor|live agent) (please|now)\b|\blive (agent|chat|person|support)\b|\bnot a (bot|robot)\b|\bsupervisor\b|\bescalate (this|my)\b")),
    ("refund_dispute", _P(r"\b(you|they) (refused|denied|rejected|declined) (my|the|to) (refund|give|issue)|refund (was|has been|got) (denied|rejected|refused)|dispute(d)? (the|my) refund|unfair(ly)? (den|reject|refus)|appeal (the|my) (refund|decision)")),
    ("gdpr_privacy_request", _P(r"\b(data subject (access )?request|right to be forgotten|erase all my (personal )?data|delete all (my|of my) personal data|subject access request|dsar)\b")),
    ("threat_or_abuse", _P(r"\b(i('ll| will) (find|hurt|kill|destroy|ruin)|kill you|hurt you|burn|bomb|you('ll| will) regret)\b")),
]


# the customer is asking us to return money (a REQUEST, in any phrasing). Used by the tool gate, the staged refund prefetch and tests.
REFUND_REQUEST_RX = _P(r"refund|money back|reimburs|credit(?:ed)? (?:it |me |my |the )?back|give (?:me )?(?:back )?my (?:money|payment)|give (?:back|me back) (?:the|my|that|this)\b|"
                       r"send (?:back|me back) (?:the|my|that|this)\b|(?:get|have) (?:the |my |that |this )?(?:\w+ ){0,3}(?:returned|reversed|credited back)|"
                       r"returned to my (?:card|account|bank)|(?:return|reverse|cancel) (?:my |the |this |that )?(?:last |latest |most recent |newest |second |duplicate(?:d)? |extra )?(?:payment|charge)|"
                       r"pay me back|chargeback|take (?:back )?(?:the |that )?(?:charge|payment) back")
_FOLLOWUP = _P(r"\b(still|again|third time|no (reply|answer|response)|nobody|no one|waiting|any update|previous (ticket|email|message)|haven'?t (heard|received)|not (fixed|resolved)|follow(ing)? up|chasing)\b")


def escalation_triggers(message: str, *, sentiment: str | None = None, tier: str | None = None, open_tickets: int = 0) -> list[str]:
    out = [name for name, rx in TRIGGERS if rx.search(message or "")]
    if sentiment == "angry":
        out.append("angry_customer")
    if sentiment in ("negative", "angry") and tier == "vip":
        out.append("vip_unhappy")
    if open_tickets >= 3 and (sentiment in ("negative", "angry") or _FOLLOWUP.search(message or "")):
        out.append("repeat_contact")  # many open tickets alone is not enough: the customer must be chasing / complaining
    return list(dict.fromkeys(out))


CRITICAL = {"legal_threat", "data_breach_security", "threat_or_abuse"}
HIGH = {"unsafe_content", "fraud_or_chargeback", "angry_customer", "vip_unhappy", "repeat_contact", "refund_dispute", "gdpr_privacy_request"}
SECURITY_QUEUE = {"data_breach_security"}


def priority_floor(reasons: list[str], tier: str | None) -> str:
    if CRITICAL & set(reasons):
        return "critical"
    if HIGH & set(reasons) or tier == "vip":
        return "high"
    return "medium" if reasons else "low"
