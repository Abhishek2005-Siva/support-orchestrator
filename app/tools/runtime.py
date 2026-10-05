"""Tool runtime: registry + the guarded execution pipeline every agent tool call goes through.

Pipeline (each step is a documented guardrail, ids in docs/guardrails.md):
  G-TOOL-01  per-agent allow-list         payments agent cannot call block_card or assign_to_human, etc.  -> blocked + security event
  G-TOOL-02  JSON + strict Pydantic args  extra="forbid", regex IDs (TXN-\\d{8}), length/range limits; errors go back
                                          to the model as a repairable message (it can retry), never as a crash
  G-TOOL-03  SQLi pre-execution scan      strict mode on identifier args, phrase-level mode on free text -> blocked + event
  G-TOOL-04  identity injection / authz   customer_id comes from the authenticated session, never from the model.
                                          If the model supplies a different customer_id -> blocked + authz_violation event
  G-TOOL-05  parameterised SQL only       (inside tool handlers; SQLAlchemy bound params)
  G-TOOL-06  deterministic policy engine  dispute / fee / transfer decisions = code reading the policies table (tools/rules.py, tools/verify.py), not model opinion
  G-TOOL-07  audit log                    every write and every blocked call -> audit_log (+ security_events)
  G-TOOL-08  budget + timeout             max tool calls per query, per-call timeout, output size cap
  G-TOOL-09  idempotent writes            retries / loops cannot create duplicate disputes, reversals, replacement cards or tickets
  G-TOOL-10  intent-gated writes          each write tool needs the customer's own words asking for it (WRITE_GATES) and, for money, the dispatcher's action_requested
  G-TOOL-13  verify before act            dispute / fee reversal / transfer cancellation need a matching verification record, re-derived at action time (tools/verify.py)
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Awaitable, Callable

from pydantic import BaseModel, ConfigDict, ValidationError

from app.guardrails import sqli
from app.guardrails.events import audit, log_security_event
from app.observability.tracing import tracer

MAX_TOOL_CALLS_PER_QUERY = 12
MAX_CALLS_PER_TOOL = 4  # stops 'search again with a different query' loops
TOOL_TIMEOUT_S = 10.0
MAX_RESULT_CHARS = 6000


class StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


@dataclass
class ToolContext:
    customer_id: str
    query_id: str
    agent: str
    role: str = "customer"
    message: str = ""  # the customer's (sanitised) message: used to intent-gate write tools
    action_requested: bool | None = None  # dispatcher's semantic verdict (None = unknown, e.g. unit tests): a QUESTION about policy must not trigger an action
    tool_calls: int = 0
    per_tool: dict = field(default_factory=dict)
    evidence: list[dict] = field(default_factory=list)
    flags: dict = field(default_factory=dict)  # e.g. requires_human_approval, blocked_calls


from app.agents.rules import BLOCK_RX, CANCEL_RX, DISPUTE_RX, FEE_RX, REPLACE_RX, UNAUTH_RX  # noqa: E402  (single shared definitions)

# G-TOOL-10: tool -> (the customer's message must match, does the dispatcher's action_requested=False veto it, what to tell the model)
WRITE_GATES = {
    "file_dispute": (lambda t: DISPUTE_RX.search(t) or UNAUTH_RX.search(t), True, "the customer did not ask us to dispute or reverse anything; explain what you found and offer it instead of filing"),
    "reverse_fee": (lambda t: FEE_RX.search(t), True, "the customer did not ask for the fee to be reversed; explain the fee and the waiver policy and offer it instead"),
    "cancel_transfer": (lambda t: CANCEL_RX.search(t), False, "the customer did not ask to cancel the transfer; explain its status instead"),
    "block_card": (lambda t: BLOCK_RX.search(t), False, "the customer did not report the card lost, stolen or compromised and did not ask to block it"),
    "request_replacement_card": (lambda t: REPLACE_RX.search(t), False, "the customer did not ask for a replacement card; offer one instead"),
}


_TICKET_INTENT = __import__("re").compile(r"ticket|escalat|engineer|follow[- ]?up|open a case|raise a case|log (a|an) (issue|case|bug)|report (a|this|the) (bug|issue|problem)|contact me|get back to me|call me|someone (to )?(look|check)", __import__("re").I)


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None
    blocked: bool = False
    source: str | list[str] | None = None
    requires_human: bool = False

    def to_message(self) -> str:
        """What the model sees as the tool message."""
        body: dict[str, Any] = {"ok": self.ok}
        if self.ok:
            body["data"] = self.data
        else:
            body["error"] = self.error
        txt = json.dumps(body, default=str)
        return txt if len(txt) <= MAX_RESULT_CHARS else txt[:MAX_RESULT_CHARS] + '…"[truncated]"}'


Handler = Callable[[ToolContext, Any], Awaitable[ToolResult]]


@dataclass
class ToolSpec:
    name: str
    description: str
    args_model: type[StrictArgs]
    handler: Handler
    agents: set[str]
    write: bool = False
    strict_keys: set[str] = field(default_factory=set)

    def openai_schema(self) -> dict:
        sch = self.args_model.model_json_schema()
        sch.pop("title", None)
        props = sch.get("properties", {})
        for p in props.values():
            p.pop("title", None)
        sch.setdefault("properties", {})
        sch["additionalProperties"] = False
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": sch}}


_REGISTRY: dict[str, ToolSpec] = {}


def register(spec: ToolSpec) -> ToolSpec:
    _REGISTRY[spec.name] = spec
    return spec


def get_spec(name: str) -> ToolSpec | None:
    return _REGISTRY.get(name)


def tools_for_agent(agent: str) -> list[ToolSpec]:
    return [s for s in _REGISTRY.values() if agent in s.agents]


def openai_tools(agent: str) -> list[dict]:
    return [s.openai_schema() for s in tools_for_agent(agent)]


_TODAY: date | None = None


async def business_today() -> date:
    """Reference date for policy windows (stored at seed time so evals are reproducible)."""
    global _TODAY
    if _TODAY is None:
        from app.db.session import get_meta
        v = await get_meta("business_today")
        _TODAY = date.fromisoformat(v) if v else date.today()
    return _TODAY


def reset_business_today():
    global _TODAY
    _TODAY = None


async def execute_tool(agent: str, name: str, raw_args: str | dict | None, ctx: ToolContext) -> ToolResult:
    async with tracer.span(f"tool.{name}", kind="tool", input={"args": raw_args, "agent": agent}) as sp:
        res = await _execute(agent, name, raw_args, ctx)
        sp.update(output={"ok": res.ok, "error": res.error, "blocked": res.blocked,
                          "data": res.data if res.ok else None},
                  level=None if res.ok else ("WARNING" if not res.blocked else "ERROR"))
        if res.ok:
            ctx.evidence.append({"tool": name, "args": raw_args, "source": res.source or f"tool:{name}", "result": res.data})
        return res


async def _execute(agent: str, name: str, raw_args, ctx: ToolContext) -> ToolResult:
    spec = get_spec(name)
    if spec is None:
        return ToolResult(False, error=f"unknown tool '{name}'. Available: {[s.name for s in tools_for_agent(agent)]}")

    # G-TOOL-01 allow-list
    if agent not in spec.agents:
        ctx.flags["blocked_calls"] = ctx.flags.get("blocked_calls", 0) + 1
        await log_security_event("tool_not_allowed", f"agent={agent} tool={name}", query_id=ctx.query_id,
                                 customer_id=ctx.customer_id)
        await audit(agent, f"tool:{name}", query_id=ctx.query_id, customer_id=ctx.customer_id, args=None, outcome="blocked")
        return ToolResult(False, error=f"tool '{name}' is not permitted for this agent", blocked=True)

    # G-TOOL-08 budget
    ctx.tool_calls += 1
    if ctx.tool_calls > MAX_TOOL_CALLS_PER_QUERY:
        return ToolResult(False, error="tool-call budget exhausted for this request; answer with what you have", blocked=True)

    ctx.per_tool[name] = ctx.per_tool.get(name, 0) + 1
    if ctx.per_tool[name] > MAX_CALLS_PER_TOOL:
        return ToolResult(False, error=f"'{name}' was already called {MAX_CALLS_PER_TOOL} times; use the results you have", blocked=True)

    # G-TOOL-02 parse
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError:
            return ToolResult(False, error="arguments are not valid JSON; send a JSON object")
    else:
        args = dict(raw_args or {})
    if not isinstance(args, dict):
        return ToolResult(False, error="arguments must be a JSON object")

    # G-TOOL-04 identity: the model must never choose whose data to read
    for key in ("customer_id", "customerId", "cust_id", "user_id", "email"):
        if key in args:
            supplied = str(args.pop(key))
            if key in ("customer_id", "customerId", "cust_id") and supplied.upper() != ctx.customer_id.upper():
                ctx.flags["blocked_calls"] = ctx.flags.get("blocked_calls", 0) + 1
                await log_security_event("authz_violation", f"agent={agent} tool={name} tried customer_id={supplied}",
                                         query_id=ctx.query_id, customer_id=ctx.customer_id)
                await audit(agent, f"tool:{name}", query_id=ctx.query_id, customer_id=ctx.customer_id,
                            args={"attempted_customer": supplied}, outcome="blocked")
                return ToolResult(False, error="you may only access the authenticated customer's data", blocked=True)

    # G-TOOL-10 intent-gated writes: an action can only run if the CUSTOMER asked for it (not on the model's initiative)
    gate = WRITE_GATES.get(name)
    if gate and ctx.message and (not gate[0](ctx.message) or (gate[1] and ctx.action_requested is False)):
        await log_security_event("unrequested_write_blocked", f"agent={agent} tool={name}", query_id=ctx.query_id, customer_id=ctx.customer_id, blocked=True)
        await audit(agent, f"tool:{name}", query_id=ctx.query_id, customer_id=ctx.customer_id, args=None, outcome="blocked")
        return ToolResult(False, error=gate[2], blocked=True)

    if name == "create_ticket" and ctx.message and not _TICKET_INTENT.search(ctx.message):
        await log_security_event("unrequested_write_blocked", f"agent={agent} tool={name}", query_id=ctx.query_id, customer_id=ctx.customer_id, blocked=True)
        await audit(agent, f"tool:{name}", query_id=ctx.query_id, customer_id=ctx.customer_id, args=None, outcome="blocked")
        return ToolResult(False, error="the customer did not ask for a ticket or follow-up; give guidance, or set needs_human=true if you cannot resolve it", blocked=True)

    # G-TOOL-03 SQLi scan (before validation, so even malformed args are inspected)
    verdict = sqli.scan_args(args, strict_keys=spec.strict_keys, free_keys=set())
    if verdict:
        ctx.flags["blocked_calls"] = ctx.flags.get("blocked_calls", 0) + 1
        await log_security_event("sqli_attempt", f"tool={name} patterns={verdict.patterns} args={json.dumps(args)[:300]}",
                                 query_id=ctx.query_id, customer_id=ctx.customer_id)
        await audit(agent, f"tool:{name}", query_id=ctx.query_id, customer_id=ctx.customer_id, args=args, outcome="blocked")
        return ToolResult(False, error="arguments rejected by security policy", blocked=True)

    # G-TOOL-02 validate
    try:
        parsed = spec.args_model.model_validate(args)
    except ValidationError as e:
        errs = "; ".join(f"{'.'.join(map(str, x['loc']))}: {x['msg']}" for x in e.errors()[:4])
        return ToolResult(False, error=f"invalid arguments: {errs}")

    # run
    try:
        for attempt in range(4):  # transient "database is locked" (SQLite single-writer) is retried; real errors are not
            try:
                res = await asyncio.wait_for(spec.handler(ctx, parsed), timeout=TOOL_TIMEOUT_S)
                break
            except Exception as e:
                if attempt < 3 and spec.write and "locked" in str(e).lower():
                    await asyncio.sleep(0.05 * (attempt + 1) + 0.05 * attempt ** 2)
                    continue
                raise
    except asyncio.TimeoutError:
        return ToolResult(False, error="tool timed out")
    except Exception as e:  # never leak internals to the model
        tracer.event("tool.exception", tool=name, error=f"{type(e).__name__}: {str(e)[:200]}")
        return ToolResult(False, error="tool failed internally")

    if spec.write:  # G-TOOL-07
        await audit(agent, f"tool:{name}", query_id=ctx.query_id, customer_id=ctx.customer_id, args=args,
                    outcome="pending_approval" if res.requires_human else ("ok" if res.ok else "error"))
    if res.requires_human:
        ctx.flags["requires_human_approval"] = True
    return res
