"""Tracing: one trace per query, one span per node / LLM call / tool call / guardrail.

Two sinks, always consistent:
  1. Local JSONL  (logs/traces/YYYY-MM-DD.jsonl)  -> always on; powers reports/ and works offline.
  2. Langfuse     -> on when LANGFUSE_* keys are set; the same spans are mirrored via the v4 SDK.

All payloads are PII-masked (G-IN-03) and truncated before leaving the process.
Usage:
    async with tracer.trace("support_query", query_id=..., customer_id=..., channel=...) as t:
        async with tracer.span("dispatcher", kind="agent", input={...}) as s:
            ...
            s.update(output=..., model=..., usage={...})
        tracer.score("validator_confidence", 0.91)
"""
from __future__ import annotations

import contextvars
import hashlib
import json
import queue
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any

from app.core.config import get_settings
from app.guardrails.pii import mask_obj

_trace_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("trace_ctx", default=None)
_span_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("span_ctx", default=None)

_MAX_STR = 1500


def _clip(obj: Any, depth: int = 0) -> Any:
    if depth > 6:
        return "…"
    if isinstance(obj, str):
        return obj if len(obj) <= _MAX_STR else obj[:_MAX_STR] + f"…[+{len(obj) - _MAX_STR}]"
    if isinstance(obj, dict):
        return {str(k): _clip(v, depth + 1) for k, v in list(obj.items())[:40]}
    if isinstance(obj, (list, tuple)):
        return [_clip(v, depth + 1) for v in list(obj)[:40]]
    if hasattr(obj, "model_dump"):
        return _clip(obj.model_dump(), depth + 1)
    if isinstance(obj, (int, float, bool)) or obj is None:
        return obj
    return _clip(str(obj), depth + 1)


def safe_payload(obj: Any) -> Any:
    return mask_obj(_clip(obj))


def hash_id(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:12]


class _Writer:
    """Background JSONL writer so tracing never blocks the event loop."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.t = threading.Thread(target=self._run, daemon=True, name="trace-writer")
        self.t.start()

    def write(self, record: dict):
        self.q.put(record)

    def _run(self):
        while True:
            rec = self.q.get()
            if rec is None:
                return
            try:
                d = get_settings().log_dir / "traces"
                d.mkdir(parents=True, exist_ok=True)
                f = d / f"{datetime.now(timezone.utc):%Y-%m-%d}.jsonl"
                with f.open("a") as fh:
                    fh.write(json.dumps(rec, default=str) + "\n")
            except Exception:  # tracing must never break the app
                pass

    def flush(self, timeout: float = 5.0):
        end = time.time() + timeout
        while not self.q.empty() and time.time() < end:
            time.sleep(0.01)
        time.sleep(0.05)


def shrink(obj: Any, depth: int = 0) -> Any:
    """Compact, already-masked view of a payload for the live UI stream (strings clipped, lists/dicts truncated)."""
    if depth > 4:
        return "…"
    if isinstance(obj, str):
        return obj if len(obj) <= 220 else obj[:220] + "…"
    if isinstance(obj, dict):
        return {k: shrink(v, depth + 1) for k, v in list(obj.items())[:14]}
    if isinstance(obj, (list, tuple)):
        return [shrink(v, depth + 1) for v in list(obj)[:5]] + (["…"] if len(obj) > 5 else [])
    return obj


class SpanHandle:
    def __init__(self, record: dict, lf_obs=None):
        self.record = record
        self._lf = lf_obs

    def update(self, *, output: Any = None, model: str | None = None, usage: dict | None = None,
               metadata: dict | None = None, level: str | None = None, status_message: str | None = None):
        r = self.record
        if output is not None:
            r["output"] = safe_payload(output)
        if model:
            r["model"] = model
        if usage:
            r["usage"] = usage
        if metadata:
            r.setdefault("metadata", {}).update(safe_payload(metadata))
        if level:
            r["level"] = level
        if status_message:
            r["status_message"] = status_message
        if self._lf is not None:
            try:
                kw: dict[str, Any] = {}
                if output is not None:
                    kw["output"] = r["output"]
                if model:
                    kw["model"] = model
                if usage:
                    kw["usage_details"] = {"input": usage.get("prompt_tokens", 0), "output": usage.get("completion_tokens", 0)}
                if metadata:
                    kw["metadata"] = r["metadata"]
                if level:
                    kw["level"] = level
                if status_message:
                    kw["status_message"] = status_message
                self._lf.update(**kw)
            except Exception:
                pass


class Tracer:
    def __init__(self):
        self._writer = _Writer()
        self._lf = None
        self._lf_tried = False
        self._subs: dict[str, list] = {}

    # ---- live subscribers (used by the SSE stream so a UI can watch a query execute) ----
    def attach(self, trace_id: str, cb):
        self._subs.setdefault(trace_id, []).append(cb)

    def detach(self, trace_id: str, cb):
        if cb in self._subs.get(trace_id, []):
            self._subs[trace_id].remove(cb)
        if not self._subs.get(trace_id):
            self._subs.pop(trace_id, None)

    def _pub(self, ctx: dict | None, ev: dict):
        if not ctx or ctx["trace_id"] not in self._subs:
            return
        ev["t"] = round((time.perf_counter() - ctx["t0"]) * 1000)
        for cb in list(self._subs.get(ctx["trace_id"], [])):
            try:
                cb(ev)
            except Exception:
                pass

    # ---- Langfuse (optional) ----
    def _langfuse(self):
        if self._lf_tried:
            return self._lf
        self._lf_tried = True
        s = get_settings()
        if s.langfuse_public_key and s.langfuse_secret_key:
            try:
                from langfuse import Langfuse
                self._lf = Langfuse(
                    public_key=s.langfuse_public_key, secret_key=s.langfuse_secret_key, host=s.langfuse_host,
                    mask=lambda data=None, **kw: mask_obj(data),
                )
            except Exception:
                self._lf = None
        return self._lf

    @property
    def langfuse_enabled(self) -> bool:
        return self._langfuse() is not None

    # ---- API ----
    @asynccontextmanager
    async def trace(self, name: str, *, query_id: str | None = None, customer_id: str | None = None,
                    channel: str | None = None, tags: list[str] | None = None, input: Any = None):
        trace_id = query_id or uuid.uuid4().hex
        ctx = {"trace_id": trace_id, "name": name, "t0": time.perf_counter(), "customer": hash_id(customer_id) if customer_id else None,
               "channel": channel, "scores": {}}
        tok = _trace_ctx.set(ctx)
        lf = self._langfuse()
        stack = None
        lf_root = None
        if lf is not None:
            try:
                from contextlib import ExitStack

                from langfuse import propagate_attributes
                stack = ExitStack()
                lf_trace_id = lf.create_trace_id(seed=trace_id)
                ctx["lf_trace_id"] = lf_trace_id
                lf_root = stack.enter_context(lf.start_as_current_observation(
                    trace_context={"trace_id": lf_trace_id}, name=name, as_type="agent", input=safe_payload(input)))
                stack.enter_context(propagate_attributes(
                    user_id=ctx["customer"], session_id=trace_id, tags=(tags or []) + ([channel] if channel else [])))
            except Exception:
                stack = None
        try:
            async with self.span(name, kind="trace", input=input, _root=True, _lf_obs=lf_root) as s:
                yield s
        finally:
            if stack is not None:
                try:
                    for k, v in ctx["scores"].items():
                        lf.create_score(trace_id=ctx["lf_trace_id"], name=k, value=v)
                    stack.close()   # export happens in the SDK's background batch thread; flush only at shutdown (never block a request)
                except Exception:
                    pass
            _trace_ctx.reset(tok)

    @asynccontextmanager
    async def span(self, name: str, *, kind: str = "span", input: Any = None, metadata: dict | None = None,
                   _root: bool = False, _lf_obs=None):
        ctx = _trace_ctx.get()
        span_id = uuid.uuid4().hex[:16]
        parent = _span_ctx.get()
        rec = {
            "trace_id": ctx["trace_id"] if ctx else None, "span_id": span_id, "parent_id": parent,
            "name": name, "kind": kind, "ts": datetime.now(timezone.utc).isoformat(),
            "input": safe_payload(input), "metadata": safe_payload(metadata or {}),
            "customer": ctx.get("customer") if ctx else None, "channel": ctx.get("channel") if ctx else None,
            "status": "ok",
        }
        tok = _span_ctx.set(span_id)
        live = kind not in ("trace", "embedding", "retriever", "span") or name.startswith("node.")
        if live:
            self._pub(ctx, {"phase": "start", "id": span_id, "parent": parent, "kind": kind, "name": name,
                            "input": None if kind == "llm" else shrink(rec["input"])})
        lf_cm, lf_obs = None, _lf_obs
        lf = self._lf if self._lf_tried else None
        if lf is not None and ctx and not _root and kind != "trace":
            try:
                as_type = {"llm": "generation", "tool": "tool", "guardrail": "guardrail", "agent": "agent",
                           "retriever": "retriever", "embedding": "embedding"}.get(kind, "span")
                lf_cm = lf.start_as_current_observation(name=name, as_type=as_type, input=rec["input"])
                lf_obs = lf_cm.__enter__()
            except Exception:
                lf_cm, lf_obs = None, None
        handle = SpanHandle(rec, lf_obs)
        t0 = time.perf_counter()
        try:
            yield handle
        except BaseException as e:  # incl. CancelledError; re-raised
            rec["status"] = "error"
            rec["error"] = f"{type(e).__name__}: {str(e)[:300]}"
            handle.update(level="ERROR", status_message=rec["error"])
            raise
        finally:
            rec["duration_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            _span_ctx.reset(tok)
            if lf_cm is not None:
                try:
                    lf_cm.__exit__(None, None, None)
                except Exception:
                    pass
            if ctx and _root:
                rec["scores"] = ctx["scores"]
            if live:
                self._pub(ctx, {"phase": "end", "id": span_id, "kind": kind, "name": name, "ms": rec["duration_ms"], "status": rec["status"],
                                "error": rec.get("error"), "model": rec.get("model"), "usage": rec.get("usage"), "output": shrink(rec.get("output"))})
            self._writer.write(rec)

    def score(self, name: str, value: float):
        ctx = _trace_ctx.get()
        if ctx is not None:
            ctx["scores"][name] = value

    def event(self, name: str, **data):
        """Point-in-time event (e.g. guardrail block) attached to the current trace."""
        ctx = _trace_ctx.get()
        self._pub(ctx, {"phase": "event", "name": name, "data": shrink(safe_payload(data))})
        self._writer.write({
            "trace_id": ctx["trace_id"] if ctx else None, "span_id": uuid.uuid4().hex[:16], "parent_id": _span_ctx.get(),
            "name": name, "kind": "event", "ts": datetime.now(timezone.utc).isoformat(), "duration_ms": 0,
            "metadata": safe_payload(data), "status": "ok",
        })

    def current_trace_id(self) -> str | None:
        ctx = _trace_ctx.get()
        return ctx["trace_id"] if ctx else None

    def flush(self):
        self._writer.flush()
        if self._lf:
            try:
                self._lf.flush()
            except Exception:
                pass


tracer = Tracer()
