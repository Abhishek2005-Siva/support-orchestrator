import asyncio, json, time
import pytest
from app.guardrails.pii import find_pii, mask_pii, foreign_pii, luhn_ok
from app.llm.structured import extract_json, parse_model, StructuredOutputError, structured_call
from app.llm.gateway import TokenBucket, LLMResult, set_gateway
from app.observability.tracing import tracer, safe_payload
from pydantic import BaseModel


# ---------- PII ----------
def test_pii_email_card_phone_ssn():
    t = "mail bob@example.com card 4111 1111 1111 1111 phone +1 415 555 0132 ssn 123-45-6789"
    kinds = {m.kind for m in find_pii(t)}
    assert {"email", "card", "phone", "ssn"} <= kinds
    masked = mask_pii(t)
    assert "bob@example.com" not in masked and "4111 1111" not in masked and "<CARD-1111>" in masked

def test_pii_no_false_positive_on_ids_and_amounts():
    t = "Invoice INV-00123456 for $1,234.50 on CUST-000123, order 20240115"
    assert find_pii(t) == []

def test_luhn_rejects_random_numbers():
    assert luhn_ok("4111111111111111") and not luhn_ok("4111111111111112")
    assert find_pii("tracking 1234567890123456") == []  # fails Luhn -> not a card

def test_secret_detection():
    assert any(m.kind == "secret" for m in find_pii("key nvapi-FAKEKEYFORTESTSONLY0123456789abcdefghij"))
    assert any(m.kind == "secret" for m in find_pii("Authorization: Bearer abcdefghijklmnopqrstuvwxyz0123"))

def test_foreign_pii_allows_own_email_only():
    assert foreign_pii("write to me@x.com", {"me@x.com"}) == []
    assert len(foreign_pii("write to other@x.com", {"me@x.com"})) == 1
    assert len(foreign_pii("card 4111 1111 1111 1111", {"4111 1111 1111 1111"})) == 1  # full card never allowed

# ---------- structured ----------
class D(BaseModel):
    intent: str
    confidence: float

def test_extract_json_variants():
    assert extract_json('{"a":1}') == {"a": 1}
    assert extract_json('```json\n{"a": {"b": 2}}\n```') == {"a": {"b": 2}}
    assert extract_json('Sure! Here: {"a": "x}y"} hope it helps') == {"a": "x}y"}
    with pytest.raises(ValueError):
        extract_json("no json here")

class FakeGW:
    def __init__(self, outs): self.outs, self.calls = outs, 0
    async def chat(self, role, messages, **kw):
        o = self.outs[min(self.calls, len(self.outs) - 1)]; self.calls += 1
        return LLMResult(content=o, model="fake")

async def test_structured_repair_loop():
    gw = FakeGW(['{"intent": "billing"}', '{"intent":"billing","confidence":0.9}'])
    set_gateway(gw)
    obj, _ = await structured_call("dispatcher", [{"role": "user", "content": "x"}], D)
    assert obj.confidence == 0.9 and gw.calls == 2
    gw2 = FakeGW(["garbage"]); set_gateway(gw2)
    with pytest.raises(StructuredOutputError):
        await structured_call("dispatcher", [{"role": "user", "content": "x"}], D)
    set_gateway(None)

# ---------- rate limiter ----------
async def test_token_bucket_throttles():
    b = TokenBucket(rpm=600, burst=2)  # 10/s
    t = time.perf_counter()
    for _ in range(6):
        await b.acquire()
    assert time.perf_counter() - t >= 0.35  # 2 free, 4 more at 10/s

# ---------- tracing ----------
async def test_trace_writes_masked_jsonl(tmp_path, monkeypatch):
    from app.core.config import get_settings
    monkeypatch.setattr(get_settings(), "log_dir", tmp_path)
    async with tracer.trace("t", query_id="q1", customer_id="CUST-000001", channel="api", input={"m": "mail a@b.com"}):
        async with tracer.span("child", kind="tool", input={"card": "4111 1111 1111 1111"}) as s:
            s.update(output="ok")
        tracer.score("conf", 0.9)
    tracer.flush()
    recs = [json.loads(l) for f in (tmp_path / "traces").glob("*.jsonl") for l in f.read_text().splitlines()]
    names = {r["name"] for r in recs}
    assert {"t", "child"} <= names
    blob = json.dumps(recs)
    assert "a@b.com" not in blob and "4111 1111" not in blob and "CUST-000001" not in blob
    child = next(r for r in recs if r["name"] == "child"); root = next(r for r in recs if r["name"] == "t")
    assert child["parent_id"] == root["span_id"] and root["scores"]["conf"] == 0.9

@pytest.mark.live
async def test_live_gateway_json_and_tools():
    from app.llm.gateway import NvidiaGateway
    gw = NvidiaGateway()
    r = await gw.chat("dispatcher", [{"role": "system", "content": 'Reply JSON {"intent":"billing|technical"}'},
                                      {"role": "user", "content": "double charge on my card"}], json_mode=True)
    assert extract_json(r.content)["intent"] == "billing"


async def test_langfuse_misconfigured_never_breaks_or_slows_requests(tmp_path, monkeypatch):
    """Langfuse keys set but host unreachable: tracing must neither raise nor block the request path."""
    import time
    from app.core.config import get_settings
    from app.observability.tracing import Tracer
    s = get_settings()
    monkeypatch.setattr(s, "log_dir", tmp_path)
    monkeypatch.setattr(s, "langfuse_public_key", "pk-lf-test"); monkeypatch.setattr(s, "langfuse_secret_key", "sk-lf-test")
    monkeypatch.setattr(s, "langfuse_host", "http://127.0.0.1:9")   # nothing listens here
    t = Tracer()
    t0 = time.perf_counter()
    for i in range(5):
        async with t.trace("q", query_id=f"lf{i}", customer_id="CUST-000001", channel="api", input={"m": "x@y.com"}):
            async with t.span("llm.x", kind="llm", input={"a": 1}) as sp:
                sp.update(output="ok", model="m", usage={"prompt_tokens": 1, "completion_tokens": 1})
            t.score("conf", 0.5)
    assert time.perf_counter() - t0 < 1.5
    t.flush()
    assert t.langfuse_enabled and len(list((tmp_path / "traces").glob("*.jsonl"))) == 1


async def test_hedge_timer_ignores_time_spent_queueing():
    """Regression: hedging fired while requests were merely waiting for the semaphore/token bucket, doubling load (31 % of calls hedged)."""
    import asyncio
    from types import SimpleNamespace
    from app.core.config import get_settings
    from app.llm.gateway import NvidiaGateway
    gw = NvidiaGateway()
    gw.s = get_settings().model_copy(update={"llm_cache": False, "llm_max_concurrency": 1})
    gw.sem = asyncio.Semaphore(1)
    gw.bucket.tokens = 100

    async def fake_create(**kw):
        await asyncio.sleep(0.3)
        msg = SimpleNamespace(content="ok", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))
    gw.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    from app.core import config
    orig = config.Settings.role
    def role(self, name):
        r = orig(self, name); return r.model_copy(update={"hedge_after_s": 0.5, "fallbacks": []})
    config.Settings.role = role
    try:
        res = await asyncio.gather(*[gw.chat("dispatcher", [{"role": "user", "content": f"x{i}"}]) for i in range(3)])  # 3 calls queue behind 1 slot: ~0.9 s total
    finally:
        config.Settings.role = orig
    assert all(r.content == "ok" for r in res) and gw.stats["hedges"] == 0, gw.stats

async def test_hedge_fires_when_a_started_request_stalls():
    import asyncio
    from types import SimpleNamespace
    from app.core.config import get_settings
    from app.llm.gateway import NvidiaGateway
    gw = NvidiaGateway()
    gw.s = get_settings().model_copy(update={"llm_cache": False})
    gw.bucket.tokens = 100
    calls = {"n": 0}

    async def fake_create(**kw):
        calls["n"] += 1
        await asyncio.sleep(2.0 if calls["n"] == 1 else 0.05)   # first attempt stalls, the hedge answers quickly
        msg = SimpleNamespace(content=f"call{calls['n']}", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))
    gw.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    from app.core import config
    orig = config.Settings.role
    config.Settings.role = lambda self, name: orig(self, name).model_copy(update={"hedge_after_s": 0.2, "fallbacks": []})
    try:
        t = time.perf_counter(); r = await gw.chat("dispatcher", [{"role": "user", "content": "x"}]); dt = time.perf_counter() - t
    finally:
        config.Settings.role = orig
    assert r.content == "call2" and dt < 1.0 and gw.stats["hedges"] == 1


async def test_retired_model_is_skipped_after_first_410():
    """2026-10-03: the primary model reached end-of-life (HTTP 410). The gateway must fall back AND stop retrying the dead model on every call."""
    import httpx
    from types import SimpleNamespace
    from openai import APIStatusError
    from app.core import config
    from app.core.config import get_settings
    from app.llm.gateway import NvidiaGateway
    gw = NvidiaGateway()
    gw.s = get_settings().model_copy(update={"llm_cache": False})
    gw.bucket.tokens = 100
    seen = []

    async def fake_create(**kw):
        seen.append(kw["model"])
        if kw["model"] == "retired/model":
            req = httpx.Request("POST", "http://x"); resp = httpx.Response(410, request=req, json={"title": "Gone"})
            raise APIStatusError("gone", response=resp, body=None)
        msg = SimpleNamespace(content="ok", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")], usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))
    gw.client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=fake_create)))
    orig = config.Settings.role
    config.Settings.role = lambda self, name: orig(self, name).model_copy(update={"model": "retired/model", "fallbacks": ["good/model"], "hedge_after_s": 0})
    try:
        r1 = await gw.chat("dispatcher", [{"role": "user", "content": "a"}])
        r2 = await gw.chat("dispatcher", [{"role": "user", "content": "b"}])
    finally:
        config.Settings.role = orig
    assert r1.content == r2.content == "ok" and r1.fallback_used
    assert seen == ["retired/model", "good/model", "good/model"]      # the dead model was tried exactly once
    assert gw.stats["dead_models"] == 1
