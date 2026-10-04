"""Output guardrails, layer 1 of the validator: deterministic checks on a candidate reply (no LLM, ~1 ms).

Each check has an id; every one is re-derived from the evidence (tool results + KB text), NOT from what the model claims.
  G-OUT-01  PII / secrets: reply must not contain other people's PII, full card numbers, SSNs, IBANs or API keys
  G-OUT-02  forbidden promises / guarantees; liability admissions
  G-OUT-03  premature or unsupported action claims: "refund approved/issued" without a matching approved refund in evidence
  G-OUT-04  numeric grounding: every $ amount, date, document id (INV-/REF-/TCK-/HRQ-/PAY-) and number-with-unit
            (e.g. "14 days", "600 requests/min") must appear in the evidence or the customer's own message
  G-OUT-05  account facts require account evidence: ids/amounts about the customer's account need a db: source
  G-OUT-06  hygiene: no tool names, prompt/rule text, JSON/code fences, role-play compliance, profanity, empty/over-long reply
  G-OUT-07  other customers' ids must never be mentioned
  G-OUT-09  unfulfilled action promises ("I'll submit the refund now" without a write action in evidence)
  G-OUT-08  unsupported NEGATIVE claims ("Orbit does not offer X") need evidence for X
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from app.guardrails.pii import foreign_pii
from app.schemas.models import ValidationIssue

MAX_REPLY_CHARS = 1400

_MONEY = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)")
_DOC_ID = re.compile(r"\b(?:INV|REF|TCK|HRQ|PAY)-\d{4,8}\b")
_CUST_ID = re.compile(r"\bCUST-\d{6}\b")
_ISO_DATE = re.compile(r"(?<!\d)(20\d{2})-(\d{2})-(\d{2})(?!\d)")  # also matches inside 2026-10-01T06:00
_MONTHS = "january february march april may june july august september october november december".split()
_MONTH_ABBR = {m[:3]: i + 1 for i, m in enumerate(_MONTHS)}
_NAT_DATE = re.compile(r"\b(" + "|".join(m[:3] + r"[a-z]*" for m in _MONTHS) + r")\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", re.I)
_NUM = re.compile(r"(?<![\w.-])\d[\d,]*(?:\.\d+)?")
_NUM_UNIT = re.compile(
    r"(?<![\w.-])(\d[\d,]*(?:\.\d+)?)\s*[- ]?(business days?|working days?|days?|hours?|hrs?|minutes?|mins?|seconds?|secs?|weeks?|months?|"
    r"requests?\s*(?:/|per)\s*min(?:ute)?|requests?|%|gb|mb|seats?|attempts?|times)\b", re.I)

_PROMISES = [re.compile(p, re.I) for p in [
    r"\bI (?:guarantee|promise|swear|assure you|can confirm you will)\b", r"\bguaranteed? (?:refund|resolution|fix|credit)",
    r"\b100\s?%\s*(?:certain|sure|guaranteed)\b", r"\byou will (?:definitely|certainly|absolutely) (?:get|receive|be refunded)\b",
    r"\bwe (?:will|'ll) (?:definitely|certainly|absolutely) (?:refund|credit|fix)\b", r"\bno matter what\b.{0,40}\b(?:refund|credit)",
]]
_ADMISSIONS = [re.compile(p, re.I) for p in [
    r"\bour (?:fault|mistake|error|negligence)\b", r"\bwe (?:are|were) (?:liable|at fault|responsible for (?:the )?(?:loss|damage|breach))\b",
    r"\bwe (?:accept|admit) (?:liability|fault|responsibility)\b", r"\byou(?:'re| are) (?:entitled|owed) (?:compensation|damages)\b",
]]
_NEGATIVE = re.compile(
    r"(?:\b(?:does not|doesn't|do not|don't|did not|isn't|is not|aren't|are not|cannot|can't|currently (?:has|have) no|has no|have no|there is no|there are no|no longer)\s+"
    r"(?:currently\s+|natively\s+|yet\s+)?(?:offer|support|provide|include|have|integrate|supports?|available|come with|work with|allow|let)\w*\s+(?:with\s+|for\s+|a\s+|an\s+|the\s+|any\s+|native\s+)*([^.;!?\n]{3,80}))", re.I)
_FIRST_PERSON = re.compile(r"\bI(?:'m| am| do| don't| cannot| can't| couldn't| could not| have no| haven't| didn't)\b|\bI (?:don't|do not|cannot|can't|couldn't|could not) ", re.I)
_HEDGED = re.compile(r"knowledge base|documentation|docs\b|help center|available (?:information|docs)|my (?:information|records)", re.I)
_STOPW = set("a an the any for with and or of to in on at is are be as it its this that your you our we orbit native natively currently yet option feature support supports supported offer offers offered".split())
_REFUND_DONE = re.compile(
    r"(?:\b(?:has|have) been (?:approved|issued|processed|refunded|completed|sent)\b|\bI(?:'ve| have) (?:approved|issued|processed|refunded)\b|"
    r"\b(?:refund|credit) (?:is|was) (?:approved|issued|processed|on its way|complete)\b|\byour refund is on (?:its|the) way\b)", re.I)
_ARCH_LEAK = re.compile(r"\bI can only (?:answer|help with|handle|assist with) (?:the )?(?:billing|technical|general|account)\b|\b(?:ask|contact|speak to|check with) (?:the |our )?(?:technical|billing|general) (?:specialist|assistant|agent|bot)\b|\banother specialist\b|\bother specialists?\b", re.I)
_TRANSFER_PROMISE = re.compile(r"\bI(?:'ll| will| need to| can| am going to|'m going to)\s+(?:need to\s+)?(?:transfer|connect|hand|pass|forward|escalate)\s+you\b", re.I)
_PROMISED_ACTION = re.compile(r"\bI(?:'ll| will|'m going to| am going to)\s+(?:now\s+|go ahead and\s+|proceed to\s+|immediately\s+)?(?:submit|process|create|file|issue|initiate|raise|open|start|send)\b", re.I)
_REFUND_FILED = re.compile(r"\bI(?:'ve| have) (?:created|opened|filed|submitted|raised) (?:a |your |the )?refund (?:request|case)\b", re.I)
_REFUND_WORD = re.compile(r"\brefund", re.I)
_INTERNAL = [re.compile(p, re.I) for p in [
    r"\bHARD RULES\b", r"\bsystem prompt\b", r"<\/?customer_message>", r"\btool (?:result|call)s?\b", r"\bfunction call\b",
    r"```", r"^\s*[{\[]\s*\"", r"\bneeds_human\b", r"\bconfidence\s*[:=]\s*\d", r"\bas an ai (?:language )?model\b", r"\bdeveloper mode\b", r"\bDAN\b", r"\bjailbr",
]]
_PROFANITY = re.compile(r"\b(?:fuck\w*|shit\w*|bastard|asshole|bitch|idiot|stupid|moron|dumb)\b", re.I)


@dataclass
class OutputContext:
    message: str = ""
    evidence: list[dict] = None  # type: ignore[assignment]
    sources: list[str] = None  # type: ignore[assignment]
    customer_id: str = ""
    allowed_pii: set[str] = None  # type: ignore[assignment]
    tool_names: set[str] = None  # type: ignore[assignment]
    is_template: bool = False  # deterministic template replies (escalation holding message) skip numeric grounding

    def __post_init__(self):
        self.evidence = self.evidence or []
        self.sources = self.sources or []
        self.allowed_pii = self.allowed_pii or set()
        self.tool_names = self.tool_names or set()


def evidence_text(ev: list[dict]) -> str:
    return json.dumps(ev, default=str).lower()


def _norm_money(s: str) -> Decimal | None:
    try:
        return Decimal(s.replace(",", ""))
    except InvalidOperation:
        return None


def _numbers(text: str) -> set[str]:
    out = set()
    for m in _NUM.findall(text):
        n = m.replace(",", "").rstrip(".")
        out.add(n)
        if "." in n:
            out.add(n.rstrip("0").rstrip("."))
    return out


def _dates(text: str) -> tuple[set[tuple[int, int]], set[str]]:
    md = set()
    for y, m, d in _ISO_DATE.findall(text):
        md.add((int(m), int(d)))
    for mon, d in _NAT_DATE.findall(text):
        md.add((_MONTH_ABBR.get(mon[:3].lower(), 0), int(d)))
    return md, {m.group() for m in _ISO_DATE.finditer(text)}


def check_output(reply: str, ctx: OutputContext) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    def add(code: str, sev: str, detail: str):
        issues.append(ValidationIssue(code=code, severity=sev, detail=detail[:300]))

    text = (reply or "").translate({0x2019: "'", 0x2018: "'", 0x201C: '"', 0x201D: '"', 0x2010: "-", 0x2011: "-", 0x202F: " ", 0x00A0: " "})
    # G-OUT-06 hygiene
    if not text.strip():
        add("empty_reply", "critical", "reply is empty")
        return issues
    if len(text) > MAX_REPLY_CHARS:
        add("reply_too_long", "warning", f"{len(text)} chars (max {MAX_REPLY_CHARS})")
    for rx in _INTERNAL:
        if rx.search(text):
            add("internal_leak", "critical", f"reply contains internal/meta text matching {rx.pattern[:40]}")
            break
    low = text.lower()
    for name in ctx.tool_names:
        if re.search(rf"\b{re.escape(name)}\b", low):
            add("tool_name_leak", "critical", f"mentions internal tool '{name}'")
            break
    if _PROFANITY.search(text):
        add("profanity", "critical", "reply contains abusive/profane language")

    # G-OUT-01 PII / secrets
    for m in foreign_pii(text, ctx.allowed_pii):
        add("pii_leak", "critical", f"reply contains {m.kind}")

    # G-OUT-07 foreign customer ids
    for cid in set(_CUST_ID.findall(text)) - {ctx.customer_id}:
        add("foreign_customer_id", "critical", f"mentions {cid}")

    # G-OUT-02 promises / admissions
    for rx in _PROMISES:
        if rx.search(text):
            add("forbidden_promise", "critical", f"matches /{rx.pattern[:50]}/")
            break
    for rx in _ADMISSIONS:
        if rx.search(text):
            add("liability_admission", "warning", f"matches /{rx.pattern[:50]}/")
            break

    ev_txt = evidence_text(ctx.evidence)
    # G-OUT-03 action claims vs evidence
    if not ctx.is_template and _REFUND_WORD.search(text):
        refunds = [e["result"] for e in ctx.evidence if e.get("tool") == "create_refund_request" and isinstance(e.get("result"), dict)]
        approved = [r for r in refunds if r.get("status") == "approved"]
        pending = [r for r in refunds if r.get("status") == "pending_approval"]
        if _REFUND_DONE.search(text) and not approved:
            add("unsupported_refund_claim" if not pending else "premature_refund_claim", "critical",
                "reply says a refund was approved/issued but evidence shows " + ("only a pending request" if pending else "no approved refund"))
        if _REFUND_FILED.search(text) and not refunds:
            add("unsupported_refund_claim", "critical", "reply says a refund request was filed but no create_refund_request evidence exists")

    if ctx.is_template:
        return issues

    if _ARCH_LEAK.search(text):
        add("internal_leak", "critical", "reply exposes the internal multi-specialist structure")
    if _TRANSFER_PROMISE.search(text) and not any(e.get("tool") == "assign_to_human" for e in ctx.evidence):
        add("unfulfilled_action_promise", "critical", "reply promises a transfer to a person but no handoff was performed")

    # G-OUT-09 unfulfilled action promise: "I'll submit the refund now" is only acceptable if the action was actually performed
    if _PROMISED_ACTION.search(text) and not any(e.get("tool") in ("create_refund_request", "create_ticket", "assign_to_human") for e in ctx.evidence):
        add("unfulfilled_action_promise", "critical", "reply promises an action (submit/process/create...) but no write action was performed")

    # G-OUT-08 unsupported NEGATIVE claims: "Orbit does not offer X" must be backed by the evidence (absence of evidence is not
    # evidence of absence). Hedged phrasing ("not mentioned in the knowledge base") is the honest form and is allowed here
    # (the uncertainty detector routes it to a human).
    ev_words = set(re.findall(r"[a-z0-9][a-z0-9\-]+", ev_txt))
    for sent in re.split(r"(?<=[.!?])\s+", text):
        m = _NEGATIVE.search(sent)
        if not m or _HEDGED.search(sent) or _FIRST_PERSON.search(sent):
            continue
        obj = [w for w in re.findall(r"[a-z0-9][a-z0-9\-]+", m.group(1).lower()) if w not in _STOPW and len(w) > 2]
        if obj and sum(w in ev_words or w.rstrip("s") in ev_words for w in obj) / len(obj) < 0.67:
            add("unsupported_negative_claim", "critical", f"claims absence without evidence: '{sent.strip()[:140]}'")

    # G-OUT-04 numeric grounding
    corpus = ev_txt + " " + (ctx.message or "").lower()
    corpus_money = {_norm_money(x) for x in _MONEY.findall(corpus)}
    for amt in _MONEY.findall(text):
        v = _norm_money(amt)
        if v is not None and v not in corpus_money:
            add("ungrounded_amount", "critical", f"${amt} does not appear in the evidence")
    for did in set(_DOC_ID.findall(text)) | set(m.group() for m in _DOC_ID.finditer(text)):
        if did.lower() not in corpus:
            add("ungrounded_id", "critical", f"{did} does not appear in the evidence")
    corpus_md, corpus_iso = _dates(corpus)
    for iso in {m.group() for m in _ISO_DATE.finditer(text)}:
        if iso not in corpus_iso:
            add("ungrounded_date", "warning", f"{iso} does not appear in the evidence")
    reply_md, _ = _dates(text)
    for md in reply_md - corpus_md:
        if md[0]:
            add("ungrounded_date", "warning", f"date month={md[0]} day={md[1]} not in evidence")
    corpus_nums = _numbers(corpus)
    for num, unit in _NUM_UNIT.findall(text):
        n = num.replace(",", "")
        if n not in corpus_nums and n.rstrip("0").rstrip(".") not in corpus_nums:
            add("ungrounded_number", "warning", f"'{num} {unit}' not found in evidence")

    # G-OUT-05 account facts need account evidence
    has_db = any(s.startswith(("db:", "diag:")) for s in ctx.sources)
    if (_DOC_ID.search(text) or _MONEY.search(text)) and not has_db and not any(s.startswith("kb:") for s in ctx.sources):
        add("ungrounded_account_facts", "critical", "reply states ids/amounts but no evidence sources exist")
    return issues


def has_critical(issues: list[ValidationIssue]) -> bool:
    return any(i.severity == "critical" for i in issues)
