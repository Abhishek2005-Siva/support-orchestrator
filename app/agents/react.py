"""Generic ReAct engine used by every tool-using specialist, built as a real LangGraph sub-graph:

        ┌──────────┐  tool_calls & iterations<cap   ┌──────────┐
 START ─►   llm    ├────────────────────────────────►  tools   │ (all tool calls of a turn run in PARALLEL)
        └────┬─────┘◄────────────────────────────────┴──────────┘
             │ no tool_calls / cap reached
             ▼
            END  -> final JSON parsed + validated (G-LLM-01) -> SpecialistResponse

Guardrails here: iteration cap (G-AGENT-01), tools exposed = per-agent allow-list (G-TOOL-01),
final-turn tools removed at the cap so the model must answer, sources derived from real tool evidence
(never trusted from the model, G-OUT-05), identity injected via ToolContext (G-TOOL-04).
"""
from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.core.config import get_settings
from app.llm.gateway import LLMResult, get_gateway
from app.llm.structured import StructuredOutputError, parse_model, structured_call
from app.observability.tracing import tracer
from app.schemas.models import ModelSpecialistOutput, SpecialistResponse
from app.tools.runtime import MAX_CALLS_PER_TOOL, ToolContext, ToolResult, execute_tool, openai_tools


UNCERTAIN = re.compile(r"(?:knowledge base|documentation|docs|our (?:records|information)) (?:does not|doesn't|do not|don't|did not|didn't) (?:mention|contain|cover|include|have|list|specif\w+)|"
                       r"\bnot (?:certain|sure)\b|\b(?:I|we)(?:'m| am| was)? (?:couldn'?t|could not|unable to|can'?t|cannot|not able to) (?:find|confirm|verify|locate)\b|\bno (?:relevant )?(?:information|article|details) (?:about|on|available|found)\b|"
                       r"\bdon'?t have (?:enough |any |specific )?(?:information|details)\b|"
                       r"\bnot (?:mentioned|listed|documented|covered|specified|stated|included)\b.{0,40}\b(?:knowledge base|documentation|docs|help center|materials)\b|"
                       r"\b(?:no|not any) (?:mention|record|reference) of\b", re.I)


_TEXT_MAP = {0x2010: "-", 0x2011: "-", 0x202F: " ", 0x00A0: " ", 0x2009: " ", 0x200A: " ",
             0x2019: "'", 0x2018: "'", 0x201C: '"', 0x201D: '"'}  # typographic quotes broke regex checks ("I\u2019ve submitted ...")


_TRAILER = re.compile(r"[\s\-—]*(?:\(?\s*)?(?:confidence|needs_human|needs_human_reason|actions_taken)\s*[:=].*$", re.I | re.S)


def clean_text(t: str) -> str:
    """Models like non-breaking spaces/hyphens ("10\u202fs", "14\u2011day"): normalise so every downstream check and the customer see plain text."""
    t = unicodedata.normalize("NFKC", t).translate(_TEXT_MAP)
    return _TRAILER.sub("", t).strip()  # the prose fallback must never leak "Confidence: 0.9, needs_human: false" to the customer


class ReactState(TypedDict):
    messages: list[dict]
    iterations: int
    ctx: Any
    tools: list[dict]
    agent: str
    last: Any


def _compact_evidence(ev: list[dict]) -> list[dict]:
    out = []
    for e in ev:
        r = e.get("result")
        if isinstance(r, dict) and "results" in r:  # KB hits: keep text short
            r = {**r, "results": [dict(h) for h in r["results"]]}  # keep the FULL chunk: truncating it hid facts from the validator (case study #10)
        out.append({"tool": e["tool"], "source": e["source"], "result": r})
    return out


def build_react_graph():
    cap = get_settings().max_react_iterations

    async def llm_node(st: ReactState):
        gw = get_gateway()
        final_turn = st["iterations"] >= cap - 1
        msgs = st["messages"]
        if final_turn:
            msgs = msgs + [{"role": "user", "content": "Tool budget reached. Give your final JSON answer now using what you have."}]
        res: LLMResult = await gw.chat("specialist", msgs, tools=None if final_turn else st["tools"],
                                       name=f"llm.{st['agent']}.step{st['iterations']}")
        return {"messages": st["messages"] + [res.assistant_message()], "iterations": st["iterations"] + 1, "last": res}

    def route(st: ReactState):
        last: LLMResult = st["last"]
        return "tools" if last.tool_calls and st["iterations"] < cap else END

    async def tools_node(st: ReactState):
        last: LLMResult = st["last"]
        ctx: ToolContext = st["ctx"]
        offered = {t["function"]["name"] for t in st["tools"]}

        async def run_one(tc):
            if tc.name not in offered:  # G-TOOL-12: the model may only execute tools that were offered this turn (stops pattern-continuation loops)
                return ToolResult(False, error=f"tool '{tc.name}' is not available now; use the information you already have and give your final answer", blocked=True)
            return await execute_tool(st["agent"], tc.name, tc.arguments, ctx)
        results = await asyncio.gather(*[run_one(tc) for tc in last.tool_calls])
        msgs = list(st["messages"])
        for tc, r in zip(last.tool_calls, results):
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": r.to_message()})
        return {"messages": msgs}

    g = StateGraph(ReactState)
    g.add_node("llm", llm_node)
    g.add_node("tools", tools_node)
    g.add_edge(START, "llm")
    g.add_conditional_edges("llm", route, {"tools": "tools", END: END})
    g.add_edge("tools", "llm")
    return g.compile()


_graph = None


def get_react_graph():
    global _graph
    if _graph is None:
        _graph = build_react_graph()
    return _graph


async def _final_output(agent: str, content: str, messages: list[dict]) -> ModelSpecialistOutput:
    """Parse the model's final turn. Order of preference (G-LLM-01):
       1. valid JSON object                      -> use it
       2. plain prose, no JSON at all            -> accept as the reply (confidence defaults; the validator judges it)
       3. broken / schema-violating JSON         -> ONE repair call in JSON mode, else StructuredOutputError"""
    try:
        return parse_model(content, ModelSpecialistOutput)
    except Exception as e:
        tracer.event("llm.final_parse_failed", agent=agent, error=str(e)[:300], content=content[:600])
        if "{" not in content and content.strip():
            return ModelSpecialistOutput(reply=content.strip(), confidence=0.7)
        parsed, _ = await structured_call("specialist", messages + [
            {"role": "user", "content": "Return your final answer now as the required JSON object only."}],
            ModelSpecialistOutput, name=f"llm.{agent}.repair")
        return parsed


async def _prefetch(agent: str, plan: list[tuple[str, dict]], ctx: ToolContext, followup=None) -> list[dict]:
    """Run independent lookups in parallel (guarded + traced like any tool call) and render them as assistant tool-call
    turns plus tool results, so the model starts with the evidence and usually needs ONE llm call instead of 4-5.
    `followup(results)` may return a second stage of lookups that depend on the first (e.g. refund eligibility per invoice)."""
    if not plan:
        return []
    msgs: list[dict] = []
    stage, n = plan, 0
    while stage and n < 5:
        calls = [(f"pre_{n}_{i}", name, args) for i, (name, args) in enumerate(stage)]
        results = await asyncio.gather(*[execute_tool(agent, nm, a, ctx) for _, nm, a in calls])
        msgs += [{"role": "assistant", "content": None,
                  "tool_calls": [{"id": cid, "type": "function", "function": {"name": nm, "arguments": json.dumps(a)}} for cid, nm, a in calls]},
                 *[{"role": "tool", "tool_call_id": cid, "content": r.to_message()} for (cid, _, _), r in zip(calls, results)]]
        stage = followup([(nm, r) for (_, nm, _), r in zip(calls, results)]) if followup else None
        n += 1
    return msgs


async def run_react_agent(agent: str, system: str, user: str, ctx: ToolContext,
                          prefetch: list[tuple[str, dict]] | None = None, followup=None) -> SpecialistResponse:
    """Run the tool loop and return a validated SpecialistResponse. Raises StructuredOutputError if no valid answer."""
    s = get_settings()
    async with tracer.span(f"agent.{agent}", kind="agent", input={"user": user[-600:]}) as sp:
        pre = await _prefetch(agent, prefetch or [], ctx, followup)
        tools = openai_tools(agent)
        done = {e["tool"] for e in ctx.evidence}
        tools = [t for t in tools if t["function"]["name"] not in ("get_customer_profile", "get_service_status", "get_user_logs") or t["function"]["name"] not in done]
        kb_ev = [e for e in ctx.evidence if e["tool"] == "search_knowledge_base" and isinstance(e.get("result"), dict)]
        if kb_ev:  # latency + accuracy: the KB was already searched with the customer's own words.
            top = max((h.get("score", 0) for e in kb_ev for h in e["result"].get("results", [])), default=0)
            if top >= s.kb_strong_score:       # strong hit -> no re-search tool at all (stops reworded-query loops)
                tools = [t for t in tools if t["function"]["name"] != "search_knowledge_base"]
            else:                              # weak/no hit -> allow exactly ONE refinement search
                ctx.per_tool["search_knowledge_base"] = max(ctx.per_tool.get("search_knowledge_base", 0), MAX_CALLS_PER_TOOL - 1)
        st: ReactState = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}, *pre],
                          "iterations": 0, "ctx": ctx, "tools": tools, "agent": agent, "last": None}
        out = await get_react_graph().ainvoke(st, config={"recursion_limit": 4 * s.max_react_iterations + 6})
        last: LLMResult = out["last"]
        parsed = await _final_output(agent, last.content, out["messages"])
        # G-AGENT-05: an honest "I don't know" must route to a human even if the model forgot to set needs_human
        first = re.split(r"(?<=[.!?])\s+", parsed.reply.strip(), maxsplit=1)[0]
        if not parsed.needs_human and UNCERTAIN.search(first):   # a caveat after a real answer is fine; an answer that IS the caveat is not
            parsed = parsed.model_copy(update={"needs_human": True, "needs_human_reason": parsed.needs_human_reason or "specialist expressed uncertainty",
                                               "confidence": min(parsed.confidence, 0.5)})
        if any(e["tool"] == "verify_transaction_issue" and isinstance(e.get("result"), dict) and e["result"].get("decision") == "human" for e in ctx.evidence) \
                and not parsed.needs_human and not ctx.flags.get("requires_human_approval"):  # a verification that says "human" is a deterministic hand-off, whatever the model wrote
            parsed = parsed.model_copy(update={"needs_human": True, "needs_human_reason": parsed.needs_human_reason or "verification requires a specialist"})
        parsed = parsed.model_copy(update={"reply": clean_text(parsed.reply)})
        sources = list(dict.fromkeys(x for e in ctx.evidence for x in (e["source"] if isinstance(e["source"], list) else [e["source"]])))
        resp = SpecialistResponse(
            agent=agent, reply=parsed.reply.strip(), sources=sources, actions_taken=parsed.actions_taken,
            confidence=parsed.confidence, needs_human=parsed.needs_human and not ctx.flags.get("requires_human_approval"),
            needs_human_reason=parsed.needs_human_reason,
            requires_human_approval=bool(ctx.flags.get("requires_human_approval")),
            evidence=_compact_evidence(ctx.evidence), iterations=out["iterations"], tool_calls=ctx.tool_calls)
        sp.update(output={"confidence": resp.confidence, "needs_human": resp.needs_human, "sources": sources,
                          "iterations": resp.iterations, "tool_calls": resp.tool_calls})
        return resp
