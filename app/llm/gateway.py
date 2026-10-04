"""LLM gateway: the ONLY place the app talks to a model provider.

Provider = NVIDIA NIM through its OpenAI-compatible endpoint (swap provider = change 3 env vars).

Reliability / latency / cost controls implemented here (documented in docs/guardrails.md):
  R-01  global asyncio.Semaphore            -> bounded concurrency on a free-tier key
  R-02  client-side token bucket (RPM)      -> avoid 429s instead of reacting to them
  R-03  retry w/ exponential backoff+jitter -> 429 (honours Retry-After), 5xx, timeouts
  R-04  model fallback chain per role       -> primary down / 404 / exhausted retries => next model
  R-05  exact-match LRU cache (temp==0)     -> repeated queries cost 0 calls / 0 ms
  R-06  per-call timeout                    -> no call can hang a request
  R-08  dead-model circuit breaker          -> 404/410/403 (retired / not served) => skip that model for 15 min instead of paying a round trip per call
  R-07  hedged requests                     -> a stalled call (>5s) is raced by an identical one; first answer wins
  L-01  thinking toggle per role            -> nemotron-3 non-thinking is ~3x faster; thinking only where it pays off
  O-01  every call is a traced "llm" span   -> model, latency, tokens, retries, fallback used
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import random
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI, RateLimitError

from app.core.config import get_settings
from app.observability.tracing import tracer


class LLMError(Exception):
    """Raised when every model in the chain failed."""


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: str  # raw JSON string as produced by the model


@dataclass
class LLMResult:
    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    latency_s: float = 0.0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached: bool = False
    retries: int = 0
    fallback_used: bool = False

    def assistant_message(self) -> dict:
        """OpenAI-format assistant message to append to the running conversation."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.content or None}
        if self.tool_calls:
            msg["tool_calls"] = [
                {"id": tc.id, "type": "function", "function": {"name": tc.name, "arguments": tc.arguments}}
                for tc in self.tool_calls
            ]
        return msg


class Gateway(Protocol):
    async def chat(self, role: str, messages: list[dict], *, tools: list[dict] | None = None,
                   json_mode: bool = False, max_tokens: int | None = None, temperature: float | None = None,
                   thinking: bool | None = None, name: str | None = None) -> LLMResult: ...

    async def embed(self, texts: list[str], input_type: str = "passage") -> list[list[float]]: ...


class TokenBucket:
    def __init__(self, rpm: int, burst: int | None = None):
        self.rate = rpm / 60.0
        self.capacity = burst or max(4, rpm // 4)
        self.tokens = float(self.capacity)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        while True:
            async with self._lock:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                wait = (1 - self.tokens) / self.rate
            await asyncio.sleep(wait)


class LRU:
    def __init__(self, size: int = 2000):
        self.size, self.d = size, OrderedDict()

    def get(self, k):
        if k in self.d:
            self.d.move_to_end(k)
            return self.d[k]

    def put(self, k, v):
        self.d[k] = v
        self.d.move_to_end(k)
        while len(self.d) > self.size:
            self.d.popitem(last=False)


_THINK_RE = re.compile(r"<think>.*?</think>", re.S)


def _supports_thinking_flag(model: str) -> bool:
    return "nemotron-3" in model


class NvidiaGateway:
    def __init__(self):
        s = get_settings()
        self.s = s
        self.client = AsyncOpenAI(base_url=s.nvidia_base_url, api_key=s.nvidia_api_key or "missing",
                                  timeout=httpx.Timeout(s.llm_timeout_s, connect=5.0), max_retries=0)
        self.sem = asyncio.Semaphore(s.llm_max_concurrency)
        self.bucket = TokenBucket(s.llm_max_rpm, burst=6)
        self.cache = LRU()
        self.stats = {"calls": 0, "cache_hits": 0, "retries": 0, "rate_limited": 0, "fallbacks": 0,
                      "errors": 0, "hedges": 0, "prompt_tokens": 0, "completion_tokens": 0}
        self.latencies: list[float] = []
        self.dead: dict[str, float] = {}   # model -> monotonic time until which it is skipped (retired / not served)

    def _mark_dead(self, model: str, why: str, minutes: float = 15.0):
        self.dead[model] = time.monotonic() + minutes * 60
        self.stats["dead_models"] = len([m for m, t in self.dead.items() if t > time.monotonic()])
        tracer.event("llm.model_unavailable", model=model, reason=why[:160], skip_minutes=minutes)

    # ---------------------------------------------------------------- chat
    async def chat(self, role: str, messages: list[dict], *, tools: list[dict] | None = None,
                   json_mode: bool = False, max_tokens: int | None = None, temperature: float | None = None,
                   thinking: bool | None = None, name: str | None = None) -> LLMResult:
        cfg = self.s.role(role)
        temp = cfg.temperature if temperature is None else temperature
        think = cfg.thinking if thinking is None else thinking
        mt = (max_tokens or cfg.max_tokens) + (1500 if think else 0)

        key = None
        if self.s.llm_cache and temp == 0:
            key = hashlib.sha256(json.dumps([role, cfg.model, messages, tools, json_mode, think, mt], sort_keys=True,
                                            default=str).encode()).hexdigest()
            hit = self.cache.get(key)
            if hit is not None:
                self.stats["cache_hits"] += 1
                async with tracer.span(name or f"llm.{role}", kind="llm", input={"messages": messages[-2:]}) as sp:
                    sp.update(output=hit.content, model=hit.model, metadata={"cached": True})
                return LLMResult(**{**hit.__dict__, "cached": True, "latency_s": 0.0})

        chain = [cfg.model, *cfg.fallbacks]
        last_err: Exception | None = None
        async with tracer.span(name or f"llm.{role}", kind="llm",
                               input={"messages": messages[-3:], "tools": [t["function"]["name"] for t in tools or []]},
                               metadata={"role": role, "thinking": think}) as sp:
            now = time.monotonic()
            live = [m for m in chain if self.dead.get(m, 0) <= now] or chain      # R-08 circuit breaker: skip models known to be gone
            for idx, model in enumerate(live):
                try:
                    res = await self._call_with_retries(model, messages, tools, json_mode, mt, temp, think, cfg.hedge_after_s)
                    res.fallback_used = idx > 0
                    if idx > 0:
                        self.stats["fallbacks"] += 1
                    sp.update(output={"content": res.content, "tool_calls": [t.name for t in res.tool_calls]},
                              model=model,
                              usage={"prompt_tokens": res.prompt_tokens, "completion_tokens": res.completion_tokens},
                              metadata={"retries": res.retries, "fallback_used": res.fallback_used,
                                        "latency_s": round(res.latency_s, 3)})
                    if key:
                        self.cache.put(key, res)
                    return res
                except LLMError as e:
                    last_err = e
                    if re.search(r"HTTP (404|410|403)\b", str(e)):   # permanent: retired (410 end-of-life), not served (404), not allowed (403)
                        self._mark_dead(model, str(e))
                    continue
            self.stats["errors"] += 1
            raise LLMError(f"all models failed for role={role}: {last_err}")

    async def _call_with_retries(self, model, messages, tools, json_mode, max_tokens, temperature, thinking, hedge_after=None) -> LLMResult:
        s = self.s
        attempt = 0
        t_total = time.perf_counter()
        while True:
            try:
                kwargs: dict[str, Any] = dict(model=model, messages=messages, temperature=temperature, max_tokens=max_tokens)
                if tools:
                    kwargs["tools"] = tools
                    kwargs["tool_choice"] = "auto"
                if json_mode and not tools:
                    kwargs["response_format"] = {"type": "json_object"}
                if _supports_thinking_flag(model):
                    kwargs["extra_body"] = {"chat_template_kwargs": {"enable_thinking": bool(thinking)}}
                r, dt = await self._hedged_post(kwargs, hedge_after)
                m = r.choices[0].message
                content = _THINK_RE.sub("", m.content or "").strip()
                tcs = [ToolCall(id=t.id, name=t.function.name, arguments=t.function.arguments or "{}")
                       for t in (m.tool_calls or [])]
                if not content and not tcs:
                    raise LLMError(f"{model}: empty response (finish_reason={r.choices[0].finish_reason})")
                u = r.usage
                pt, ct = (u.prompt_tokens, u.completion_tokens) if u else (0, 0)
                self.stats["prompt_tokens"] += pt
                self.stats["completion_tokens"] += ct
                self.latencies.append(dt)
                return LLMResult(content=content, tool_calls=tcs, model=model, latency_s=time.perf_counter() - t_total,
                                 prompt_tokens=pt, completion_tokens=ct, retries=attempt)
            except RateLimitError as e:
                self.stats["rate_limited"] += 1
                retry_after = None
                try:
                    retry_after = float(e.response.headers.get("retry-after", ""))
                except Exception:
                    pass
                err: Exception = e
                delay = retry_after if retry_after else min(8.0, 1.0 * 2 ** attempt) + random.random() * 0.5
            except (APITimeoutError, APIConnectionError) as e:
                err, delay = e, min(4.0, 0.5 * 2 ** attempt) + random.random() * 0.3
            except APIStatusError as e:
                if e.status_code >= 500:
                    err, delay = e, min(4.0, 0.5 * 2 ** attempt) + random.random() * 0.3
                else:  # 400/401/403/404: retrying the same model is pointless
                    raise LLMError(f"{model}: HTTP {e.status_code} {str(e)[:160]}") from e
            except LLMError:
                raise
            attempt += 1
            self.stats["retries"] += 1
            if attempt > s.llm_max_retries:
                raise LLMError(f"{model}: gave up after {attempt} attempts: {type(err).__name__}") from err
            await asyncio.sleep(delay)

    async def _post(self, kwargs: dict, started: asyncio.Event | None = None):
        await self.bucket.acquire()
        async with self.sem:
            if started is not None:
                started.set()   # the hedge timer must not count time spent queueing behind the rate limiter
            t0 = time.perf_counter()
            self.stats["calls"] += 1
            r = await self.client.chat.completions.create(**kwargs)
            return r, time.perf_counter() - t0

    async def _hedged_post(self, kwargs: dict, hedge_after: float | None = None):
        """R-07 hedged request: the free endpoint stalls ~3% of calls for 20-30s. If the first attempt has not answered
        after `llm_hedge_after_s`, send an identical second request and use whichever finishes first."""
        h = hedge_after if hedge_after is not None else self.s.llm_hedge_after_s
        started = asyncio.Event()
        t1 = asyncio.create_task(self._post(kwargs, started))
        if h <= 0:
            return await t1
        waiter = asyncio.create_task(started.wait())
        await asyncio.wait({t1, waiter}, return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        if not t1.done():
            done, _ = await asyncio.wait({t1}, timeout=h)
            if done:
                return t1.result()
        else:
            return t1.result()
        self.stats["hedges"] += 1
        t2 = asyncio.create_task(self._post(kwargs))
        pending, errors = {t1, t2}, []
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for d in done:
                    if d.exception() is None:
                        return d.result()
                    errors.append(d.exception())
            raise errors[0]
        finally:
            for p in pending:
                p.cancel()
            await asyncio.gather(t1, t2, return_exceptions=True)   # consume every outcome (no "exception was never retrieved" noise)

    # ---------------------------------------------------------------- embeddings
    async def embed(self, texts: list[str], input_type: str = "passage") -> list[list[float]]:
        out: list[list[float]] = []
        async with tracer.span("embed", kind="embedding", input={"n": len(texts), "input_type": input_type}):
            for i in range(0, len(texts), 32):
                batch = texts[i:i + 32]
                attempt = 0
                while True:
                    await self.bucket.acquire()
                    try:
                        async with self.sem:
                            r = await self.client.embeddings.create(
                                model=self.s.embed_model, input=batch,
                                extra_body={"input_type": input_type, "truncate": "END"})
                        out.extend(d.embedding for d in r.data)
                        break
                    except (RateLimitError, APITimeoutError, APIConnectionError) as e:
                        attempt += 1
                        if attempt > self.s.llm_max_retries:
                            raise LLMError(f"embedding failed: {type(e).__name__}") from e
                        await asyncio.sleep(min(8.0, 2 ** attempt) + random.random())
                    except APIStatusError as e:
                        if e.status_code >= 500 and attempt < self.s.llm_max_retries:
                            attempt += 1
                            await asyncio.sleep(1 + attempt)
                            continue
                        raise LLMError(f"embedding HTTP {e.status_code}") from e
        return out

    async def aclose(self):
        await self.client.close()


_gateway: Gateway | None = None


def get_gateway() -> Gateway:
    global _gateway
    if _gateway is None:
        if get_settings().llm_mode == "mock":
            from app.llm.mock import MockGateway
            _gateway = MockGateway()
        else:
            _gateway = NvidiaGateway()
    return _gateway


def set_gateway(gw: Gateway | None):
    """Test hook / mode switch."""
    global _gateway
    _gateway = gw
