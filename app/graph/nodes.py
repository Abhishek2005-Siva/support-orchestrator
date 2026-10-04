"""LangGraph node functions. Each node returns a partial state update; every node is traced as a span.

Guardrail / reliability map (ids in docs/guardrails.md):
  intake        G-IN-01..07  input sanitising, secret masking, SQLi + injection checks (+ profile/ticket prefetch in parallel)
  safety_check  G-IN-08      NVIDIA content-safety model, runs IN PARALLEL with the dispatcher (no added latency), fails open
  triage        G-AGENT-01/02/03  topic control + deterministic escalation overlay
  specialist    each specialist failure is contained (becomes needs_human) so one bad branch cannot crash the request
  validator     G-VAL-*      deterministic + LLM judge; revise loop capped at max_revisions
  human_*       G-HITL-01    review row + interrupt(): the graph is paused (checkpointed) until a human resolves it
"""
from __future__ import annotations

import asyncio
import re

from langgraph.types import interrupt

from app.agents.dispatcher import dispatch
from app.agents.escalation import holding_reply, run_escalation
from app.agents.specialists import fetch_profile, run_specialist
from app.agents.validator import validate
from app.core.config import get_settings
from app.graph import helpers as H
from app.graph.cache import answer_cache, cacheable
from app.graph.state import SupportState
from app.guardrails.events import log_security_event
from app.guardrails.input import check_input
from app.llm.gateway import get_gateway
from app.observability.tracing import tracer
from app.schemas.models import SpecialistResponse

OFF_TOPIC_REPLY = ("I'm here to help with Orbit: billing, your account and technical questions. I can't help with that request, "
                   "but if you have a question about Orbit I'm happy to help.")
FOREIGN_REPLY = "I can only help with your own account, so I can't look up or share information about other accounts."
MERGE_SYSTEM = ("You combine partial answers from several support specialists into ONE coherent reply to the customer. "
                "Keep every id, amount, date and number EXACTLY as written; add NO new facts, promises or guarantees; remove duplicates; "
                "at most ~120 words; no sign-off. Output ONLY the reply text.\nThe partial answers follow.")


# ------------------------------------------------------------------ intake
async def intake(state: SupportState) -> dict:
    cid = state["customer_id"]
    async with tracer.span("node.intake", kind="span"):
        verdict, profile, tickets = await asyncio.gather(
            check_input(state["message"], customer_id=cid), fetch_profile(cid), H.count_open_tickets(cid))
        out: dict = {"clean_message": verdict.message, "profile": profile, "open_tickets": tickets,
                     "input": {"reasons": verdict.reasons, "injection_score": verdict.injection_score,
                               "patterns": verdict.injection_patterns + verdict.sqli_patterns, "foreign": verdict.foreign_customer_ids,
                               "secrets_masked": verdict.secrets_masked, "truncated": verdict.truncated, "llm_gray": verdict.used_llm_classifier}}
        if not verdict.blocked and get_settings().answer_cache_enabled:
            from app.tools.kb import get_kb
            try:
                kbv = (await get_kb())._kb_hash()
            except Exception:
                kbv = "?"
            ck = answer_cache.key(verdict.message, profile, kbv)
            out["cache_key"] = ck
            hit = answer_cache.get(ck)
            if hit:
                out["cache_hit"] = hit
        if verdict.blocked:
            kind = "sql_injection" if "sql_injection" in verdict.reasons else "prompt_injection"
            if "empty_message" not in verdict.reasons:
                await log_security_event(kind, f"patterns={verdict.injection_patterns + verdict.sqli_patterns} msg={state['message'][:200]}",
                                         query_id=state["query_id"], customer_id=cid)
            out.update(status="rejected", final_reply=verdict.refusal_reply)
        elif verdict.foreign_customer_ids:
            await log_security_event("authz_probe", f"message references {verdict.foreign_customer_ids}", query_id=state["query_id"],
                                     customer_id=cid, blocked=False)
        return out


def after_intake(state: SupportState):
    if state.get("status") == "rejected":
        return ["finalize_rejected"]
    if state.get("cache_hit"):
        return ["serve_cache"]
    return ["dispatcher", "safety_check", "kb_warm"]


async def serve_cache(state: SupportState) -> dict:
    h = state["cache_hit"]
    tracer.event("cache.hit", intents=h.get("intents"))
    return {"final_reply": h["reply"], "status": "delivered", "dispatch": h.get("dispatch") or {"intents": h.get("intents", [])},
            "flags": {"cache_hit": True}}


async def finalize_rejected(state: SupportState) -> dict:
    return {"status": "rejected"}


# ------------------------------------------------------------------ parallel triage branches
async def dispatcher_node(state: SupportState) -> dict:
    dec = await dispatch(state["clean_message"], profile=state.get("profile"), history=state.get("history"),
                         open_tickets=state.get("open_tickets", 0))
    return {"dispatch": dec.model_dump()}


async def safety_check(state: SupportState) -> dict:
    s = get_settings()
    if not s.enable_safety_model or s.llm_mode != "live":
        return {"safety": {"checked": False}}
    async with tracer.span("guard.safety_model", kind="guardrail", input={"len": len(state["clean_message"])}) as sp:
        try:
            r = await asyncio.wait_for(get_gateway().chat("safety", [{"role": "user", "content": state["clean_message"][:1500]}], max_tokens=20, temperature=0),
                                       timeout=s.safety_deadline_s)
            unsafe = bool(re.search(r"unsafe", r.content, re.I))
            sp.update(output={"unsafe": unsafe, "raw": r.content[:60]})
            return {"safety": {"checked": True, "unsafe": unsafe, "raw": r.content[:80]}}
        except Exception as e:  # fail OPEN: the safety model is an extra layer, other guardrails still apply
            sp.update(output={"error": str(e)[:100]})
            return {"safety": {"checked": False, "error": str(e)[:100]}}


async def kb_warm(state: SupportState) -> dict:
    """Embed the raw message while the dispatcher runs, so the specialist's KB search is a cache hit (latency)."""
    try:
        from app.tools.kb import get_kb
        kb = await get_kb()
        await kb._qvec(state["clean_message"][:300])
    except Exception:
        pass
    return {}


async def triage(state: SupportState) -> dict:
    dec = dict(state["dispatch"])
    safety = state.get("safety") or {}
    if safety.get("unsafe"):
        reasons = list(dec.get("forced_escalation_reasons", []))
        if "unsafe_content" not in reasons:
            reasons.append("unsafe_content")
        intents = [i for i in dec["intents"] if i != "off_topic"]
        if "escalation" not in intents:
            intents.append("escalation")
        dec.update(intents=intents, forced_escalation_reasons=reasons)
        await log_security_event("unsafe_content", f"safety model flagged: {safety.get('raw')}", query_id=state["query_id"],
                                 customer_id=state["customer_id"], blocked=False)
    return {"dispatch": dec, "retry_count": 0, "feedback": [], "specialist_outputs": ["RESET"]}


def route_after_triage(state: SupportState):
    from langgraph.types import Send
    intents = state["dispatch"]["intents"]
    if intents == ["off_topic"]:
        return "off_topic_reply"
    sends = []
    base = {k: state.get(k) for k in ("query_id", "customer_id", "profile", "dispatch", "history")}
    base["message"] = state["clean_message"]
    base["foreign"] = (state.get("input") or {}).get("foreign") or []
    base["feedback"] = []
    for i in intents:
        if i in ("billing", "technical", "general"):
            sends.append(Send("specialist", {**base, "agent": i, "sub_question": (state["dispatch"].get("sub_questions") or {}).get(i)}))
        elif i == "escalation":
            sends.append(Send("escalation_node", {**base, "reasons": state["dispatch"].get("forced_escalation_reasons", [])}))
    return sends or [Send("specialist", {**base, "agent": "general"})]


async def off_topic_reply(state: SupportState) -> dict:
    foreign = (state.get("input") or {}).get("foreign")
    return {"final_reply": FOREIGN_REPLY if foreign else OFF_TOPIC_REPLY, "status": "delivered", "flags": {"off_topic": True}}


# ------------------------------------------------------------------ specialists
async def specialist_node(arg: dict) -> dict:
    agent = arg["agent"]
    try:
        r = await run_specialist(agent, message=arg["message"], customer_id=arg["customer_id"], query_id=arg["query_id"],
                                 profile=arg.get("profile"), dispatch=arg.get("dispatch"), history=arg.get("history"),
                                 feedback=arg.get("feedback"), foreign_ids=arg.get("foreign"), sub_question=arg.get("sub_question"))
        return {"specialist_outputs": [r.model_dump()]}
    except Exception as e:  # contain failures: this branch becomes a "needs human" output
        tracer.event("specialist.failed", agent=agent, error=f"{type(e).__name__}: {str(e)[:200]}")
        r = SpecialistResponse(agent=agent, reply="(unavailable)", confidence=0.0, needs_human=True,
                               needs_human_reason=f"{agent} specialist failed: {type(e).__name__}")
        return {"specialist_outputs": [r.model_dump()]}


async def escalation_node(arg: dict) -> dict:
    try:
        r = await run_escalation(message=arg["message"], customer_id=arg["customer_id"], query_id=arg["query_id"], profile=arg.get("profile"),
                                 dispatch=arg.get("dispatch"), history=arg.get("history"), reasons=arg.get("reasons") or [])
        return {"escalation_output": r.model_dump()}
    except Exception as e:
        tracer.event("escalation.failed", error=f"{type(e).__name__}: {str(e)[:200]}")
        r = SpecialistResponse(agent="escalation", reply="I'm sorry for the trouble. I'm passing your case to a human specialist who will follow up shortly.",
                               confidence=0.5, needs_human=True, needs_human_reason="escalation agent error")
        return {"escalation_output": r.model_dump()}


# ------------------------------------------------------------------ merge
async def merge(state: SupportState) -> dict:
    async with tracer.span("node.merge", kind="span") as sp:
        outs = [SpecialistResponse(**o) for o in state.get("specialist_outputs", []) if isinstance(o, dict)]
        esc = SpecialistResponse(**state["escalation_output"]) if state.get("escalation_output") else None
        evidence = [e for o in outs for e in o.evidence] + (esc.evidence if esc else [])
        sources = list(dict.fromkeys(x for o in outs + ([esc] if esc else []) for x in o.sources))
        useful = [o for o in outs if o.reply and o.reply != "(unavailable)"]
        if not outs and esc:  # escalation only: deterministic template reply
            merged = {"reply": esc.reply, "is_template": True, "evidence": evidence, "sources": sources, "confidence": esc.confidence,
                      "needs_human": False, "requires_human_approval": False, "agents": ["escalation"]}
        else:
            if len(useful) == 1:
                body = useful[0].reply
            elif len(useful) > 1 and get_settings().merge_mode != "llm":
                order = {"billing": 0, "technical": 1, "general": 2}
                body = "\n\n".join(o.reply for o in sorted(useful, key=lambda o: order.get(o.agent, 9)))  # template merge: no extra LLM round trip
            elif len(useful) > 1:
                try:
                    body = (await get_gateway().chat("merge", [{"role": "system", "content": MERGE_SYSTEM + " (combine partial answers)"},
                                                              {"role": "user", "content": "\n".join(f"---\n{o.reply}\n" for o in useful)}],
                                                     name="llm.merge")).content.strip()
                except Exception:
                    body = " ".join(o.reply for o in useful)
            else:
                body = ""
            reply = f"{esc.reply}\n\n{body}".strip() if (esc and body) else (body or (esc.reply if esc else ""))
            merged = {"reply": reply, "is_template": False, "evidence": evidence, "sources": sources,
                      "confidence": min([o.confidence for o in outs] or [0.5]),
                      "needs_human": any(o.needs_human for o in outs) or not useful,
                      "requires_human_approval": any(o.requires_human_approval for o in outs),
                      "agents": [o.agent for o in outs] + (["escalation"] if esc else [])}
            if esc:
                # escalation holding text is a template: grounding check on the combined text still uses all evidence
                merged["is_template"] = False
        sp.update(output={"agents": merged["agents"], "chars": len(merged["reply"])})
        return {"merged": merged}


# ------------------------------------------------------------------ validator
async def validator_node(state: SupportState) -> dict:
    m = state["merged"]
    prof = state.get("profile") or {}
    res = await validate(message=state["clean_message"], reply=m["reply"], evidence=m["evidence"], sources=m["sources"],
                         customer_id=state["customer_id"], specialist_confidence=m["confidence"],
                         specialist_needs_human=bool(m["needs_human"]) and not m["is_template"],
                         revision=state.get("retry_count", 0), is_template=m["is_template"])
    return {"validation": res.model_dump()}


def route_after_validation(state: SupportState):
    v = state["validation"]["verdict"]
    has_specialists = any(a not in ("escalation",) for a in state["merged"].get("agents", []))
    if v == "approve":
        return "deliver"
    if v == "revise" and has_specialists and state.get("retry_count", 0) < get_settings().max_revisions:
        return "revise"
    return "human_prepare"


async def revise(state: SupportState) -> dict:
    fb = [f"[{i['code']}] {i['detail']}" for i in state["validation"]["issues"] if i["severity"] in ("critical", "warning")][:6]
    tracer.event("graph.revise", attempt=state.get("retry_count", 0) + 1, feedback=fb)
    return {"retry_count": state.get("retry_count", 0) + 1, "feedback": fb, "specialist_outputs": ["RESET"]}


def route_revise(state: SupportState):
    from langgraph.types import Send
    base = {k: state.get(k) for k in ("query_id", "customer_id", "profile", "dispatch", "history")}
    base["message"] = state["clean_message"]
    base["foreign"] = (state.get("input") or {}).get("foreign") or []
    base["feedback"] = state.get("feedback") or []
    subq = (state["dispatch"].get("sub_questions") or {})
    return [Send("specialist", {**base, "agent": a, "sub_question": subq.get(a)}) for a in state["merged"]["agents"] if a != "escalation"]


# ------------------------------------------------------------------ deliver / human review
async def deliver(state: SupportState) -> dict:
    m = state["merged"]
    flags: dict = {}
    status, review_id = "delivered", None
    if m.get("requires_human_approval"):
        review_id = await H.file_approval_review(
            query_id=state["query_id"], customer_id=state["customer_id"], message=state["clean_message"],
            summary="Refund request above the auto-approval limit awaiting human approval. Customer was told approval is pending.", reply=m["reply"])
        flags.update(approval_review_id=review_id, requires_human_approval=True)
    if state.get("escalation_output"):  # escalated: the customer gets the holding reply now, a human follows up out-of-band
        row = await H.get_review(state["query_id"])
        review_id = row.id if row else review_id
        status = "human_review"
        flags["escalated"] = True
    top_kb = max((h.get("score", 0) for e in m["evidence"] if e.get("tool") == "search_knowledge_base" and isinstance(e.get("result"), dict)
                  for h in e["result"].get("results", [])), default=0.0)
    if (state.get("cache_key") and status == "delivered" and cacheable(state["clean_message"], m["reply"], m["sources"], state.get("profile"),
                                                                       bool(state.get("escalation_output")), bool(m.get("requires_human_approval")),
                                                                       top_kb, bool(m.get("needs_human")))):
        answer_cache.put(state["cache_key"], {"reply": m["reply"], "intents": (state.get("dispatch") or {}).get("intents", []),
                                              "dispatch": state.get("dispatch")})
        flags["cached_for_next_time"] = True
    return {"final_reply": m["reply"], "status": status, "review_id": review_id, "flags": flags}


async def human_prepare(state: SupportState) -> dict:
    m = state.get("merged") or {"reply": "", "evidence": [], "sources": [], "agents": []}
    val = state.get("validation") or {}
    reasons = ["validator_hand_off"] + ([i["code"] for i in val.get("issues", []) if i["severity"] == "critical"][:3])
    esc = state.get("escalation_output")
    notes = [f"Draft reply (not sent): {m.get('reply', '')[:500]}", f"Validator verdict={val.get('verdict')} confidence={val.get('confidence')} issues="
             + ", ".join(f"{i['severity']}:{i['code']}" for i in val.get("issues", [])[:6])]
    if not esc:
        out = await run_escalation(message=state["clean_message"], customer_id=state["customer_id"], query_id=state["query_id"],
                                   profile=state.get("profile"), dispatch=state.get("dispatch"), history=state.get("history"),
                                   reasons=reasons, context_notes=notes)
        esc = out.model_dump()
    row = await H.get_review(state["query_id"])
    await H.set_review_draft(state["query_id"], draft=m.get("reply"), holding=esc["reply"], reason="; ".join(reasons)[:200])
    return {"escalation_output": esc, "review_id": row.id if row else None, "holding_reply": esc["reply"], "final_reply": esc["reply"],
            "status": "human_review"}


async def human_wait(state: SupportState) -> dict:
    decision = interrupt({"review_id": state.get("review_id"), "query_id": state["query_id"], "customer_id": state["customer_id"],
                          "draft": (state.get("merged") or {}).get("reply"), "holding_reply": state.get("holding_reply")})
    return {"human_decision": decision}


async def human_resolve(state: SupportState) -> dict:
    d = state["human_decision"]
    action = d.get("action")
    draft = (state.get("merged") or {}).get("reply")
    if action == "approve" and draft and draft != "(unavailable)":
        final, st, rstatus = draft, "delivered", "approved"
    elif action == "edit" and d.get("reply"):
        final, st, rstatus = d["reply"], "delivered", "edited"
    else:
        final, st, rstatus = "Thank you for your patience. After review we are unable to assist further with this request through chat; please contact support@orbit.example.", "rejected", "rejected"
    if state.get("review_id"):
        await H.resolve_review_row(state["review_id"], status=rstatus, reviewer=d.get("reviewer", "staff"), note=d.get("note"), final_reply=final)
    return {"final_reply": final, "status": st, "flags": {"human_resolved": rstatus}}
