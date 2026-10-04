"""PII / secret detection and masking.

Used in three places (see docs/guardrails.md):
  G-IN-03  mask PII before text is written to logs / Langfuse traces
  G-OUT-01 detect PII in a draft reply that does not belong to the current customer
  G-IN-04  detect secrets (API keys, JWTs) pasted by the user so they are never echoed

Pure regex + Luhn (no ML dependency) => microseconds of latency, deterministic, testable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PIIMatch:
    kind: str
    value: str
    start: int
    end: int


_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_CARD = re.compile(r"(?<![\w-])(?:\d[ -]?){13,19}(?![\w-])")
_SSN = re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}(?:[ ]?[A-Z0-9]{1,3})?\b")
_PHONE = re.compile(r"(?<![\w-])(?:\+\d[\d ()\-.]{7,}\d|\(?\d{3}\)?[ .\-]\d{3}[ .\-]\d{4})(?![\w-])")
_IPV4 = re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b")
_SECRET = re.compile(
    r"\b(?:nvapi-[A-Za-z0-9_\-]{20,}|sk-[A-Za-z0-9_\-]{20,}|xox[abpr]-[A-Za-z0-9\-]{10,}|"
    r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}|AKIA[0-9A-Z]{16})\b"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{20,}")


def luhn_ok(digits: str) -> bool:
    nums = [int(c) for c in digits if c.isdigit()]
    if not 13 <= len(nums) <= 19:
        return False
    total = 0
    for i, n in enumerate(reversed(nums)):
        if i % 2 == 1:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def find_pii(text: str) -> list[PIIMatch]:
    """Return all PII/secret matches (overlaps resolved: secrets > card > email > ssn > iban > phone > ip)."""
    if not text:
        return []
    found: list[PIIMatch] = []
    taken: list[tuple[int, int]] = []

    def add(kind: str, rx: re.Pattern, validator=None):
        for m in rx.finditer(text):
            if any(m.start() < e and m.end() > s for s, e in taken):
                continue
            if validator and not validator(m.group()):
                continue
            found.append(PIIMatch(kind, m.group(), m.start(), m.end()))
            taken.append((m.start(), m.end()))

    add("secret", _SECRET)
    add("secret", _BEARER)
    add("card", _CARD, luhn_ok)
    add("email", _EMAIL)
    add("ssn", _SSN)
    add("iban", _IBAN)
    add("phone", _PHONE, lambda v: sum(c.isdigit() for c in v) >= 9)
    add("ip", _IPV4)
    return sorted(found, key=lambda m: m.start)


def mask_pii(text: str, *, keep_last4_card: bool = True, kinds: set[str] | None = None) -> str:
    """Replace PII with typed placeholders, e.g. <EMAIL>, <CARD-4242>. `kinds` limits which types are masked."""
    if not text:
        return text
    out, last = [], 0
    for m in find_pii(text):
        if kinds is not None and m.kind not in kinds:
            continue
        out.append(text[last:m.start])
        if m.kind == "card" and keep_last4_card:
            out.append("<CARD-" + re.sub(r"\D", "", m.value)[-4:] + ">")
        else:
            out.append(f"<{m.kind.upper()}>")
        last = m.end
    out.append(text[last:])
    return "".join(out)


def mask_obj(obj, _depth: int = 0):
    """Recursively mask strings inside dict/list payloads (for trace input/output)."""
    if _depth > 8:
        return obj
    if isinstance(obj, str):
        return mask_pii(obj)
    if isinstance(obj, dict):
        return {k: mask_obj(v, _depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [mask_obj(v, _depth + 1) for v in obj]
    return obj


def foreign_pii(text: str, allowed: set[str]) -> list[PIIMatch]:
    """PII in `text` whose value is not in the allow-set (the current customer's own email/phone, etc.).

    Cards are never allowed in full (only last-4 may be shown), secrets are never allowed.
    """
    allowed_l = {a.lower() for a in allowed if a}
    bad = []
    for m in find_pii(text):
        if m.kind in ("secret", "card", "ssn", "iban"):
            bad.append(m)
        elif m.value.lower() not in allowed_l:
            bad.append(m)
    return bad
