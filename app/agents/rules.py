"""Deterministic escalation triggers (no LLM). They OVERRIDE the model's routing so safety-critical cases can never be
mis-routed by a classification slip. Guardrail id: G-AGENT-02. Each trigger returns a stable reason code."""
from __future__ import annotations

import re

_P = lambda p: re.compile(p, re.I)
TRIGGERS: list[tuple[str, re.Pattern]] = [
    ("legal_threat", _P(r"\b(lawyer|attorney|solicitor|sue\b|suing|lawsuit|legal action|take legal|court|small claims|legal department|my legal team|ombudsman|consumer protection|ftc|cfpb|attorney general|bbb)\b")),
    ("regulator_or_formal_complaint", _P(r"\b(regulator|regulatory|file a (formal )?complaint|formal complaint|lodge a complaint|complaint (about|against)|report (you|the bank) to)\b")),
    ("account_takeover_or_scam", _P(r"\b(identity theft|phishing|scam(med)?|social engineering|someone (opened|took out) (an? )?(account|loan|card) in my name|"
                                   r"(my )?(online banking|account|login|password|app|profile) (was|has been|got|is) (hacked|compromised|taken over|accessed)|someone (logged|got|broke) (in)?to my (account|online banking|app)|"
                                   r"(gave|shared|told|sent) (them|someone|the caller|a stranger|the scammer) my (pin|password|otp|code|details)|authori[sz]ed push payment|tricked me into)\b")),
    ("explicit_human_request", _P(r"\b(speak|speaking|talk|talking|chat|chatting|connect me|transfer me|escalate|get me|put me through|hand me over)\b.{0,30}\b(human|real person|person|agent|assistant|advisor|operator|employee|staff|team member|manager|supervisor|representative|someone|specialist)\b|\b(human|real person|manager|supervisor|live agent) (please|now)\b|\blive (agent|chat|person|support)\b|\bnot a (bot|robot|machine)\b")),
    ("decision_appeal", _P(r"\b(you|they|the bank) (refused|denied|rejected|declined|closed) (my|the) (dispute|claim|refund|appeal|case)|(dispute|claim|refund|case) (was|has been|got) (denied|rejected|refused|closed)|unfair(ly)? (den|reject|refus)|appeal (the|my) (decision|dispute|claim)")),
    ("account_restriction", _P(r"\b(closed|froze|frozen|restricted|suspended|blocked|locked) my (bank )?account|my account (is|was|has been) (closed|frozen|restricted|suspended|blocked|locked)|why (is|was) my account (closed|frozen|restricted)\b")),
    ("bereavement", _P(r"\b(passed away|has died|deceased|bereavement|death of|late (husband|wife|father|mother|spouse)|executor of (the|an) estate)\b")),
    ("gdpr_privacy_request", _P(r"\b(data subject (access )?request|right to be forgotten|erase all my (personal )?data|delete all (my|of my) personal data|subject access request|dsar)\b")),
    ("threat_or_abuse", _P(r"\b(i('ll| will) (find|hurt|kill|destroy|ruin)|kill you|hurt you|burn|bomb|you('ll| will) regret)\b")),
    ("sensitive_aml_topic", _P(r"\b(money laundering|structuring|why (is|was) (my|the) (wire|transfer|deposit) (under review|being reviewed|held|on hold|flagged)|source of (funds|wealth) (review|check)|compliance (hold|review))\b")),
]


# ---- per-tool intent gates: a write needs the CUSTOMER's own words asking for it (not the model's initiative) ----
UNAUTH_RX = _P(r"unauthori[sz]ed|don'?t recogni[sz]e|do not recogni[sz]e|not recogni[sz]|didn'?t (make|do|authori[sz]e|approve)|did not (make|authori[sz]e|approve)|not (me|mine)\b|fraud|someone (used|has used|stole|took)|wasn'?t me|never (made|authori[sz]ed)")
DISPUTE_RX = _P(r"refund|money back|get (it|that|this|the (money|charge|payment)) back|(want|need|like) (it|that|this|the (charge|payment|money)|my money) (back|removed|reversed|returned)|dispute|charge ?back|revers|credit (me|it|that|the)|reimburs|"
                r"remove (the |this |that )?(duplicate|extra|second|double|charge)|(fix|sort|resolve|correct) (this|it|that|the)\b|cancel (the )?(second|duplicate|double|extra)|give (me )?(back )?my money|take (it|that|the charge) (off|back)|pay me back")
FEE_RX = _P(r"waive|revers|refund|remove|take (it |that |the fee )?off|credit (it |me |that )?back|reimburs|money back|get (it|that|my money) back|can (you|i) (get|have) (it|that|the fee)|cancel (the )?fee|drop (the )?fee")
CANCEL_RX = _P(r"cancel|stop (the |this |that |my )?(transfer|payment|wire)|recall|call off|undo|revers|don'?t want (it|this|that) to (go|be sent)|abort|halt")
BLOCK_RX = _P(r"\blost\b|stolen|stole|missing|compromis|fraud|\bblock|freez|frozen|\block\b|disable|swallowed|suspicious|hack|unauthori[sz]ed|don'?t recogni[sz]e|do not recogni[sz]e|not recogni[sz]|someone (used|has used|took)|not (me|mine)\b|didn'?t (make|authori[sz]e)|did not (make|authori[sz]e)|wasn'?t me|misplaced|can'?t find my card|left my card|pickpocket|mugged|robbed")
REPLACE_RX = _P(r"replace|replacement|new card|re-?issue|send me a (new )?card|order (me )?a (new )?card|another card")
# the customer is asking us to ACT on money (any phrasing); used by the staged prefetch and tests
ACTION_REQUEST_RX = _P(DISPUTE_RX.pattern + "|" + UNAUTH_RX.pattern)
REFUND_REQUEST_RX = ACTION_REQUEST_RX  # legacy alias
_FOLLOWUP = _P(r"\b(still|again|third time|no (reply|answer|response)|nobody|no one|waiting|any update|previous (ticket|email|message)|haven'?t (heard|received)|not (fixed|resolved)|follow(ing)? up|chasing)\b")


def escalation_triggers(message: str, *, sentiment: str | None = None, tier: str | None = None, open_tickets: int = 0) -> list[str]:  # tier: "private" is the top segment
    out = [name for name, rx in TRIGGERS if rx.search(message or "")]
    if sentiment == "angry":
        out.append("angry_customer")
    if sentiment in ("negative", "angry") and tier == "private":
        out.append("vip_unhappy")
    if open_tickets >= 3 and (sentiment in ("negative", "angry") or _FOLLOWUP.search(message or "")):
        out.append("repeat_contact")  # many open tickets alone is not enough: the customer must be chasing / complaining
    return list(dict.fromkeys(out))


CRITICAL = {"legal_threat", "account_takeover_or_scam", "threat_or_abuse", "bereavement"}
HIGH = {"unsafe_content", "regulator_or_formal_complaint", "angry_customer", "vip_unhappy", "repeat_contact", "decision_appeal", "gdpr_privacy_request", "account_restriction", "sensitive_aml_topic"}
SECURITY_QUEUE = {"account_takeover_or_scam"}
COMPLIANCE_QUEUE = {"sensitive_aml_topic", "account_restriction"}


def priority_floor(reasons: list[str], tier: str | None) -> str:
    if CRITICAL & set(reasons):
        return "critical"
    if HIGH & set(reasons) or tier == "private":
        return "high"
    return "medium" if reasons else "low"
