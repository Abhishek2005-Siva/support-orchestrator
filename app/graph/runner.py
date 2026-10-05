"""Run a support query through the graph, with a hard timeout and a SAFE FALLBACK:
any unhandled failure (LLM down, DB error, timeout) => the customer gets an apologetic holding reply and the case goes
to the human review queue. The graph can therefore never "spin forever" or return a 500 to a customer. (G-REL-01)"""
from __future__ import annotations

import asyncio
import json
import time
from langgraph.types import Command

from app.agents.escalation import run_escalation
from app.core.config import get_settings
from app.graph import helpers as H
from app.graph.builder import build_graph
from app.guardrails.pii import mask_pii
from app.observability.tracing import tracer

_graph = None


def get_graph():
    global _graph
    if _graph is None:
        _graph = build_graph()
    return _graph


def set_graph(g):
    global _graph
    _graph = g


def init_graph(checkpointer=None):
    set_graph(build_graph(checkpointer))
    return _graph


SAFE_REPLY = "I'm sorry, I ran into a problem handling your request. I've passed it to a human specialist who will follow up with you shortly."


async def _fallback(query_id: str, customer_id: str, message: str, error: str) -> dict:
    tracer.event("graph.fallback", error=error[:300])
    try:
        esc = await asyncio.wait_for(run_escalation(message=message, customer_id=customer_id, query_id=query_id, profile=None, dispatch=None,
                                                    history=None, reasons=["system_error"], context_notes=[f"System error: {error[:200]}"]), 20)
        row = await H.get_review(query_id)
        return {"final_reply": esc.reply, "status": "human_review", "review_id": row.id if row else None,
                "flags": {"fallback": True, "error": error[:200]}}
    except Exception as e:
        return {"final_reply": SAFE_REPLY, "status": "human_review", "review_id": None,
                "flags": {"fallback": True, "error": f"{error[:100]} / {type(e).__name__}"}}


def _result(query_id: str, st: dict, latency_ms: int) -> dict:
    dispatch = st.get("dispatch") or {}
    val = st.get("validation") or {}
    flags = dict(st.get("flags") or {})
    inp = st.get("input") or {}
    if inp.get("reasons"):
        flags["input_reasons"] = inp["reasons"]
    if dispatch.get("forced_escalation_reasons"):
        flags["escalation_reasons"] = dispatch["forced_escalation_reasons"]
    if st.get("retry_count"):
        flags["revisions"] = st["retry_count"]
    return {"query_id": query_id, "status": st.get("status", "error"), "reply": st.get("final_reply"),
            "intents": dispatch.get("intents", []), "review_id": st.get("review_id"), "latency_ms": latency_ms, "flags": flags,
            "validation": {"verdict": val.get("verdict"), "confidence": val.get("confidence"), "layer": val.get("layer"),
                           "issues": [f"{i['severity']}:{i['code']}" for i in val.get("issues", [])]} if val else None,
            "dispatch": {k: dispatch.get(k) for k in ("intents", "urgency", "sentiment", "confidence")} if dispatch else None,
            "agents": (st.get("merged") or {}).get("agents", [])}


async def _persist(query_id, customer_id, channel, message, res: dict):
    await H.upsert_conversation(query_id, customer_id, channel, message, status=res["status"], final_reply=res["reply"],
                                result_json=mask_pii(json.dumps({k: v for k, v in res.items() if k != "reply"}, default=str)),
                                latency_ms=res["latency_ms"])


async def run_query(*, query_id: str, customer_id: str, message: str, channel: str = "api", history: list[dict] | None = None,
                    timeout: float | None = None, channel_ref: str | None = None) -> dict:
    s = get_settings()
    graph = get_graph()
    config = {"configurable": {"thread_id": query_id}, "recursion_limit": 60}
    t0 = time.perf_counter()
    async with tracer.trace("support_query", query_id=query_id, customer_id=customer_id, channel=channel, input={"message": message}) as root:
        await H.upsert_conversation(query_id, customer_id, channel, message, channel_ref=channel_ref)
        try:
            out = await asyncio.wait_for(graph.ainvoke({"query_id": query_id, "customer_id": customer_id, "channel": channel,
                                                        "message": message, "history": history or []}, config),
                                         timeout or s.request_timeout_s)
        except Exception as e:  # incl. TimeoutError
            out = await _fallback(query_id, customer_id, message, f"{type(e).__name__}: {e}")
        res = _result(query_id, out, int((time.perf_counter() - t0) * 1000))
        await _persist(query_id, customer_id, channel, message, res)
        rid = res["review_id"] or res["flags"].get("approval_review_id")
        if rid and (res["status"] == "human_review" or res["flags"].get("requires_human_approval")):
            try:  # never let alerting break the customer's response
                from app.integrations.slack import notify_review
                await notify_review(rid)
            except Exception:
                pass
        tracer.score("human_review_flag", 1.0 if res["status"] == "human_review" else 0.0)
        tracer.score("rejected_flag", 1.0 if res["status"] == "rejected" else 0.0)
        tracer.score("latency_ms", float(res["latency_ms"]))
        root.update(output={"status": res["status"], "intents": res["intents"], "validation": res["validation"], "flags": res["flags"],
                            "reply": res["reply"]})
        return res


async def resume_review(*, review_id: str, action: str, reviewer: str, reply: str | None = None, note: str | None = None) -> dict:
    """Human decision for a paused conversation: approve (send draft) | edit (send `reply`) | reject."""
    row = await H.get_review_by_id(review_id)
    if row is None:
        raise KeyError(review_id)
    if row.status != "pending":
        raise ValueError(f"review already {row.status}")
    graph = get_graph()
    config = {"configurable": {"thread_id": row.query_id}, "recursion_limit": 60}
    t0 = time.perf_counter()
    decision = {"action": action, "reviewer": reviewer, "reply": reply, "note": note}
    snap = await graph.aget_state(config)
    async with tracer.trace("review_resume", query_id=row.query_id, customer_id=row.customer_id, channel="staff", input=decision):
        if snap and snap.next:
            out = await graph.ainvoke(Command(resume=decision), config)
        elif row.reason and row.reason.startswith("dispute approval needed") and action in ("approve", "reject"):
            # dispute-approval case (the customer already got a truthful "pending approval" reply): settle the dispute and tell them the outcome
            disputes = await H.settle_pending_disputes(row.customer_id, action == "approve", reviewer)
            amt = f"${disputes[0].amount_cents / 100:,.2f}" if disputes else "the disputed amount"
            did = disputes[0].id if disputes else ""
            final = (f"Good news: our disputes team approved dispute {did}. A provisional credit of {amt} has been posted to your account while we complete the investigation."
                     if action == "approve" else f"Our disputes team reviewed dispute {did} and could not approve a provisional credit{': ' + note if note else '.'} You can reply here if you'd like us to look again.")
            await H.resolve_review_row(review_id, status="approved" if action == "approve" else "rejected", reviewer=reviewer, note=note, final_reply=final)
            out = {"final_reply": final, "status": "delivered", "review_id": review_id, "flags": {"human_resolved": action, "dispute_settled": [d.id for d in disputes]}}
        else:  # no checkpoint (e.g. a restarted in-memory graph): apply the decision directly
            draft = row.draft_reply
            final = draft if action == "approve" and draft else reply if action == "edit" and reply else None
            rstatus = {"approve": "approved", "edit": "edited"}.get(action, "rejected") if final else "rejected"
            await H.resolve_review_row(review_id, status=rstatus, reviewer=reviewer, note=note, final_reply=final)
            out = {"final_reply": final or "Thank you for your patience. After review we are unable to assist further with this request through chat.",
                   "status": "delivered" if final else "rejected", "review_id": review_id, "flags": {"human_resolved": rstatus, "no_checkpoint": True}}
        res = _result(row.query_id, {**(snap.values if snap else {}), **out}, int((time.perf_counter() - t0) * 1000))
        await _persist(row.query_id, row.customer_id, "api", "(resumed)", res)
        return res


async def get_state(query_id: str) -> dict | None:
    snap = await get_graph().aget_state({"configurable": {"thread_id": query_id}})
    return snap.values if snap and snap.values else None
