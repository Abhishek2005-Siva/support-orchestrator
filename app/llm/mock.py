"""Deterministic mock LLM (LLM_MODE=mock) for offline tests, CI and load tests.

It is a rule-based stand-in with the SAME interface as the NVIDIA gateway, so everything else (tools, guardrails, graph,
validator deterministic layer, API, DB) runs for real. It dispatches on markers in the system prompt, builds replies
only from tool results already in the conversation (so grounded replies pass the validator), and sleeps
`mock_latency_ms` per call to emulate network time. NOT used for accuracy numbers - those come from the live model.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re

import numpy as np

from app.core.config import get_settings
from app.llm.gateway import LLMError, LLMResult, ToolCall
from app.observability.tracing import tracer

_ANGRY = re.compile(r"furious|outrageous|unacceptable|ridiculous|!!!|livid|useless|idiots?|scam|sue you|worst", re.I)
_NEG = re.compile(r"annoyed|frustrat|again|still not|disappoint|angry|upset|terrible", re.I)
_OFF = re.compile(r"weather|recipe|poem|football|joke|pirate|lasagna|cake|ignore (all )?(previous|your) instructions|system prompt|\bdan\b|other customers?'? (emails|data)", re.I)
_KW = {
    "billing": re.compile(r"charge|invoice|bill|refund|payment|paid|price|pricing|plan|subscription|cancel|card|vat|tax|promo|upgrade|downgrade|money back", re.I),
    "technical": re.compile(r"error|api|webhook|sso|saml|sdk|crash|dashboard|slow|timeout|export|integration|login|password|2fa|bug|outage|401|429|500|down\b", re.I),
}


def _tok(text: str) -> list[str]:
    return re.findall(r"[a-z0-9_]+", text.lower())


class MockGateway:
    def __init__(self):
        self.s = get_settings()
        self.stats = {"calls": 0, "cache_hits": 0, "retries": 0, "rate_limited": 0, "fallbacks": 0, "errors": 0, "hedges": 0,
                      "prompt_tokens": 0, "completion_tokens": 0}
        self.latencies: list[float] = []

    async def aclose(self):
        pass

    async def _sleep(self):
        base = self.s.mock_latency_ms / 1000
        await asyncio.sleep(base * (0.6 + 0.8 * random.random()))

    # ------------------------------------------------------------------ chat
    async def chat(self, role: str, messages: list[dict], *, tools: list[dict] | None = None, json_mode: bool = False,
                   max_tokens: int | None = None, temperature: float | None = None, thinking: bool | None = None,
                   name: str | None = None) -> LLMResult:
        async with tracer.span(name or f"llm.{role}", kind="llm", input={"messages": messages[-2:]}, metadata={"role": role, "mock": True}) as sp:
            await self._sleep()
            self.stats["calls"] += 1
            if self.s.mock_fail_rate and random.random() < self.s.mock_fail_rate:
                self.stats["errors"] += 1
                raise LLMError("mock: injected failure")
            sys_prompt = messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
            out = self._route(role, sys_prompt, messages, tools)
            pt = sum(len(str(m.get("content") or "")) for m in messages) // 4
            ct = len(out.content or "") // 4 + 10 * len(out.tool_calls)
            out.prompt_tokens, out.completion_tokens, out.model, out.latency_s = pt, ct, "mock", self.s.mock_latency_ms / 1000
            self.stats["prompt_tokens"] += pt
            self.stats["completion_tokens"] += ct
            sp.update(output={"content": out.content[:200], "tool_calls": [t.name for t in out.tool_calls]}, model="mock",
                      usage={"prompt_tokens": pt, "completion_tokens": ct})
            return out

    def _route(self, role: str, sys: str, messages: list[dict], tools) -> LLMResult:
        user = next((m["content"] for m in reversed(messages) if m["role"] == "user"), "") or ""
        if "triage dispatcher" in sys:
            return self._dispatch(user)
        if "strict quality auditor" in sys:
            return LLMResult(json.dumps({"unsupported_claims": [], "policy_violations": [], "answers_question": True, "tone_ok": True,
                                         "needs_human": False, "confidence": 0.92}))
        if "security classifier" in sys:
            return LLMResult(json.dumps({"injection": False, "confidence": 0.9}))
        if "HANDOFF NOTE" in sys:
            m = re.search(r'<customer_message>\n(.*?)\n</customer_message>', user, re.S)
            return LLMResult(json.dumps({"summary": f"Customer wrote: {(m.group(1) if m else user)[:200]}", "reason": "customer needs a human",
                                         "suggested_priority": "high" if _ANGRY.search(user) else "medium",
                                         "suggested_queue": "escalations", "recommended_next_step": "Review account and reply personally."}))
        if "combine" in sys.lower() and "partial answers" in sys.lower():
            parts = re.findall(r"---\n(.*?)\n(?=---|\Z)", user, re.S)
            return LLMResult(" ".join(p.strip() for p in parts) if parts else user[:400])
        agent = "billing" if "BILLING specialist" in sys else "technical" if "TECHNICAL support" in sys else "general" if "GENERAL support" in sys else None
        if agent:
            return self._specialist(agent, messages, tools)
        return LLMResult(json.dumps({"ok": True}))

    # ---- dispatcher
    def _dispatch(self, user: str) -> LLMResult:
        m = re.search(r'CUSTOMER MESSAGE:\n"""\n(.*?)\n"""', user, re.S)
        msg = m.group(1) if m else user
        if _OFF.search(msg):
            intents = ["off_topic"]
        else:
            intents = [k for k, rx in _KW.items() if rx.search(msg)] or ["general"]
        refund_req = bool(re.search(r"please refund|refund (my|the|me)|money back|reimburse", msg, re.I) and not re.search(r"policy|how long|what is", msg, re.I))
        sent = "angry" if _ANGRY.search(msg) else "negative" if _NEG.search(msg) else "neutral"
        return LLMResult(json.dumps({"intents": intents[:2], "urgency": "high" if sent != "neutral" else "medium", "sentiment": sent,
                                     "confidence": 0.9, "reasoning": "mock keyword classifier", "refund_requested": refund_req}))

    # ---- specialists
    def _specialist(self, agent: str, messages: list[dict], tools) -> LLMResult:
        tool_msgs = [m for m in messages if m["role"] == "tool"]
        results: dict[str, dict] = {}
        for tm in tool_msgs:
            # map by tool name using the assistant tool_calls that preceded
            pass
        names = {}
        for m in messages:
            for tc in (m.get("tool_calls") or []):
                names[tc["id"]] = tc["function"]["name"]
        for tm in tool_msgs:
            try:
                results[names.get(tm["tool_call_id"], "?")] = json.loads(tm["content"])
            except Exception:
                pass
        user = next((m["content"] for m in messages if m["role"] == "user"), "")
        cm = re.search(r"<customer_message>\n(.*?)\n</customer_message>", user, re.S)
        msg = (cm.group(1) if cm else user).lower()

        if agent == "billing" and tools:
            inv = (results.get("get_invoices") or {}).get("data", {}).get("invoices", [])
            if re.search(r"refund|money back", msg) and inv and "check_refund_eligibility" not in results:
                return LLMResult("", [ToolCall("m1", "check_refund_eligibility", json.dumps({"invoice_id": inv[0]["invoice_id"]}))])
            el = (results.get("check_refund_eligibility") or {}).get("data")
            if el and el.get("eligible") and "create_refund_request" not in results and re.search(r"refund|money back", msg):
                return LLMResult("", [ToolCall("m2", "create_refund_request", json.dumps({"invoice_id": el["invoice_id"], "reason": "customer requested refund"}))])
        return LLMResult(json.dumps(self._compose(agent, results, msg)))

    def _compose(self, agent: str, results: dict, msg: str) -> dict:
        lines, needs_human, conf = [], False, 0.88
        kb = (results.get("search_knowledge_base") or {}).get("data") or {}
        if agent == "billing":
            inv = (results.get("get_invoices") or {}).get("data", {}).get("invoices", [])
            rf = (results.get("create_refund_request") or {}).get("data")
            el = (results.get("check_refund_eligibility") or {}).get("data")
            if rf:
                if rf["status"] == "pending_approval":
                    lines.append(f"I've submitted refund request {rf['refund_id']} for {rf['amount']}. A billing specialist needs to approve it, usually within 1 business day.")
                else:
                    lines.append(f"Your refund request {rf['refund_id']} for {rf['amount']} has been approved; expect it in 5-10 business days.")
            elif el and not el.get("eligible"):
                lines.append(f"Invoice {el['invoice_id']} isn't eligible for a refund: {el['explanation']}")
            elif inv:
                i = inv[0]
                lines.append(f"Your latest invoice {i['invoice_id']} for {i['amount']} has status {i['status']}.")
        elif agent == "technical":
            logs = (results.get("get_user_logs") or {}).get("data", {})
            if logs.get("by_code"):
                c = logs["by_code"][0]
                lines.append(f"Your account shows {c['count']} {c['code']} events recently.")
        if kb.get("results"):
            top = kb["results"][0]
            first = [l for l in top["text"].split("\n")[1:] if l.strip()][:1]
            lines.append(f"{top['title']}: {first[0].strip('-* ')[:220] if first else ''}")
        elif kb.get("no_relevant_article"):
            needs_human, conf = True, 0.3
            lines.append("I'm not certain about that, so I'll have a specialist follow up.")
        if not lines:
            lines.append("Thanks for reaching out. I'm happy to help; could you share a few more details?")
        return {"reply": " ".join(lines), "confidence": conf, "needs_human": needs_human,
                "needs_human_reason": "no relevant article" if needs_human else None, "actions_taken": []}

    # ------------------------------------------------------------------ embeddings (hashed bag-of-words, 256-d)
    async def embed(self, texts: list[str], input_type: str = "passage") -> list[list[float]]:
        await asyncio.sleep(self.s.mock_latency_ms / 4000)
        out = []
        for t in texts:
            v = np.zeros(256, dtype=np.float32)
            for w in _tok(t):
                v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 256] += 1.0
            n = np.linalg.norm(v)
            out.append((v / n if n else v).tolist())
        return out
