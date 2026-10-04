"""Validated-answer cache (latency, G-LAT-01).

Only replies that (1) passed the validator, (2) are grounded purely in knowledge-base articles (+ the customer's own plan/tier
profile), (3) contain no PII, ids or the customer's name, and (4) did not escalate are cached. Exact-match on the normalised
question + plan + tier + account status + KB version; TTL-bounded. A hit skips every LLM call (a few ms instead of ~5-8 s).
Replies that depend on account data (invoices, logs, refunds) are never cached.
"""
from __future__ import annotations

import hashlib
import re
import time
from collections import OrderedDict

from app.core.config import get_settings
from app.guardrails.pii import find_pii

_PUNCT = re.compile(r"[^a-z0-9 ]+")
_ID = re.compile(r"\b(?:INV|CUST|REF|TCK|HRQ|PAY)-\d+\b", re.I)


class AnswerCache:
    def __init__(self, size: int = 2000):
        self.size, self.d = size, OrderedDict()
        self.hits = self.misses = self.puts = 0

    @staticmethod
    def key(message: str, profile: dict | None, kb_version: str) -> str:
        norm = " ".join(_PUNCT.sub(" ", message.lower()).split())
        p = profile or {}
        raw = "|".join([norm, str(p.get("plan")), str(p.get("tier")), str(p.get("account_status")), kb_version])
        return hashlib.sha256(raw.encode()).hexdigest()

    def get(self, k: str):
        e = self.d.get(k)
        if e and time.time() - e["t"] < get_settings().answer_cache_ttl_s:
            self.d.move_to_end(k)
            self.hits += 1
            return e["v"]
        if e:
            del self.d[k]
        self.misses += 1
        return None

    def put(self, k: str, v: dict):
        self.d[k] = {"t": time.time(), "v": v}
        self.d.move_to_end(k)
        self.puts += 1
        while len(self.d) > self.size:
            self.d.popitem(last=False)

    def stats(self) -> dict:
        tot = self.hits + self.misses
        return {"size": len(self.d), "hits": self.hits, "misses": self.misses, "puts": self.puts, "hit_rate": round(self.hits / tot, 3) if tot else 0.0}


def cacheable(message: str, reply: str, sources: list[str], profile: dict | None, escalated: bool, approval: bool, top_kb_score: float = 1.0,
              needs_human: bool = False) -> bool:
    if escalated or approval or needs_human or not reply or top_kb_score < get_settings().kb_strong_score:
        return False
    if find_pii(message) or find_pii(reply) or _ID.search(message) or _ID.search(reply):
        return False
    if any(not (s.startswith("kb:") or s in ("db:customer", "ctx:today")) for s in sources):
        return False
    if not any(s.startswith("kb:") and s != "kb:none" for s in sources):
        return False
    name = (profile or {}).get("name") or ""
    return not any(len(t) > 2 and t.lower() in reply.lower() for t in name.split())


answer_cache = AnswerCache()
