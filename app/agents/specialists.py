"""Billing / Technical / General specialists = ReAct engine + agent-specific prompt, tools and context."""
from __future__ import annotations

import re

from app.agents import prompts
from app.agents.react import run_react_agent
from app.schemas.models import SpecialistResponse
from app.tools.handlers import h_profile, NoArgs
from app.tools.runtime import ToolContext, business_today


async def fetch_profile(customer_id: str) -> dict:
    """Deterministic prefetch (saves one LLM round-trip per specialist): customer tier/plan/status."""
    ctx = ToolContext(customer_id=customer_id, query_id="prefetch", agent="general")
    r = await h_profile(ctx, NoArgs())
    return r.data if r.ok else {}


def prefetch_plan(agent: str, message: str, sub_question: str | None = None) -> list[tuple[str, dict]]:
    q = (sub_question or message).strip()[:300]
    if len(q) < 3:
        q = "help"
    kb = ("search_knowledge_base", {"query": q})
    return {"technical": [kb, ("get_service_status", {}), ("get_user_logs", {"window_hours": 168})],
            "billing": [("get_invoices", {"limit": 5}), kb],
            "general": [kb]}[agent]


_REFUNDISH = re.compile(r"refund|money back|reimburs|charged (me )?(twice|two|again|double)|double[- ]?charg|duplicate|overcharg|\bcredit back|chargeback|"
                        r"twice|two (identical )?(charges|payments)|taken twice|reverse|give back|send back|returned", re.I)


from app.agents.rules import REFUND_REQUEST_RX as _REFUND_ASK  # noqa: E402


def billing_followup(message: str, refund_requested: bool = False):
    """Staged deterministic prefetch for refund-ish messages (all reads except the last, policy-gated step):
       stage 2: refund eligibility for the newest invoice(s)        (policy engine)
       stage 3: IF the dispatcher flagged an explicit refund REQUEST, the message has refund intent, and an invoice is eligible,
                file create_refund_request through the normal guarded tool path (G-TOOL-06/09/10). The model then only explains
                the outcome, instead of sometimes promising an action it never performs."""
    if not _REFUNDISH.search(message):
        return None
    dup = re.search(r"twice|two (times|charges)|double|duplicate|again|overcharg", message, re.I)

    def fn(results):
        names = {n for n, _ in results}
        if "get_invoices" in names:
            for name, r in results:
                if name == "get_invoices" and r.ok and r.data["invoices"]:
                    ids = [i["invoice_id"] for i in r.data["invoices"][: 2 if dup else 1]]
                    return [("check_refund_eligibility", {"invoice_id": i}) for i in ids]
            return None
        if names == {"check_refund_eligibility"} and refund_requested and _REFUND_ASK.search(message):
            ok = [r.data for _, r in results if r.ok and r.data.get("eligible") and not r.data.get("existing_refund_request")]
            if ok:
                pick = next((d for d in ok if d["reason_code"] == "duplicate_charge"), ok[0])
                return [("create_refund_request", {"invoice_id": pick["invoice_id"], "reason": (message.strip() or "customer requested a refund")[:250]})]
        return None
    return fn


async def run_specialist(agent: str, *, message: str, customer_id: str, query_id: str, profile: dict | None = None,
                         dispatch: dict | None = None, history: list[dict] | None = None, feedback: list[str] | None = None,
                         foreign_ids: list[str] | None = None, ctx: ToolContext | None = None, sub_question: str | None = None) -> SpecialistResponse:
    profile = profile if profile is not None else await fetch_profile(customer_id)
    ctx = ctx or ToolContext(customer_id=customer_id, query_id=query_id, agent=agent, message=message,
                             refund_requested=(dispatch or {}).get("refund_requested") if dispatch else None)
    if profile:  # the verified profile is shown to the model in its prompt, so it must be visible to the validator as evidence
        ctx.evidence.append({"tool": "get_customer_profile", "args": {}, "source": "db:customer", "result": profile})
    today = (await business_today()).isoformat()
    ctx.evidence.append({"tool": "context", "args": {}, "source": "ctx:today", "result": {"today": today}})
    user = prompts.specialist_user(message, profile, today, dispatch, history, feedback, foreign_ids, agent, sub_question)
    return await run_react_agent(agent, prompts.specialist_system(agent), user, ctx, prefetch=prefetch_plan(agent, message, sub_question),
                                  followup=billing_followup(message, bool((dispatch or {}).get("refund_requested"))) if agent == "billing" else None)
