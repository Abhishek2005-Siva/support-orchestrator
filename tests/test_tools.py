import json
import pytest
from sqlalchemy import select
from app.db import models as m
from app.db.session import session_scope
from app.tools import handlers  # noqa: registers tools
from app.tools.runtime import ToolContext, execute_tool, openai_tools, MAX_TOOL_CALLS_PER_QUERY
from tests.conftest import customers_with, MANIFEST


def ctx(cid, agent="billing", qid="q-test"):
    return ToolContext(customer_id=cid, query_id=qid, agent=agent)

def facts(cid): return MANIFEST["customers"][cid]["facts"]

async def events(kind=None):
    async with session_scope() as s:
        q = select(m.SecurityEvent)
        if kind: q = q.where(m.SecurityEvent.kind == kind)
        return (await s.execute(q)).scalars().all()


# ---------- guardrails ----------
async def test_allow_list_blocks_and_logs(db):
    c = ctx(customers_with("double_charge")[0], agent="billing")
    r = await execute_tool("billing", "assign_to_human", {"queue": "billing", "priority": "low", "reason": "x" * 6, "summary": "y" * 12}, c)
    assert r.blocked and not r.ok
    assert len(await events("tool_not_allowed")) == 1

async def test_model_cannot_choose_customer(db):
    me, other = customers_with("double_charge")[0], customers_with("failed_payment")[0]
    c = ctx(me)
    r = await execute_tool("billing", "get_invoices", {"limit": 3, "customer_id": other}, c)
    assert r.blocked and len(await events("authz_violation")) == 1
    # same id as session is accepted and silently dropped; data returned is the session customer's
    r = await execute_tool("billing", "get_invoices", {"limit": 3, "customer_id": me}, ctx(me))
    assert r.ok and r.data["count"] == 3

async def test_cannot_read_other_customers_invoice(db):
    me, other = customers_with("double_charge")[0], customers_with("failed_payment")[0]
    other_inv = facts(other)["invoice_id"]
    r = await execute_tool("billing", "get_payment_status", {"invoice_id": other_inv}, ctx(me))
    assert not r.ok and "no such invoice" in r.error
    r2 = await execute_tool("billing", "get_payment_status", {"invoice_id": "INV-99999999"}, ctx(me))
    assert r2.error == r.error      # identical message: no enumeration oracle

@pytest.mark.parametrize("payload", ["INV-00000001' OR '1'='1", "INV-00000001; DROP TABLE invoices", "INV-1 UNION SELECT 1", "%27%20OR%201%3D1"])
async def test_sqli_in_identifier_blocked_before_db(db, payload):
    r = await execute_tool("billing", "get_payment_status", {"invoice_id": payload}, ctx(customers_with("double_charge")[0]))
    assert r.blocked and not r.ok
    assert len(await events("sqli_attempt")) == 1
    async with session_scope() as s:   # tables intact
        assert (await s.execute(select(m.Invoice).limit(1))).scalars().first() is not None

async def test_sqli_in_freetext_blocked_but_benign_sql_words_pass(db, kb_bm25):
    c = ctx(customers_with("double_charge")[0], agent="technical")
    r = await execute_tool("technical", "search_knowledge_base", {"query": "x' UNION SELECT password FROM users --"}, c)
    assert r.blocked
    r = await execute_tool("technical", "search_knowledge_base", {"query": "how do I select the pro plan and drop my old subscription"}, c)
    assert r.ok

async def test_invalid_args_return_repairable_error(db):
    c = ctx(customers_with("double_charge")[0])
    r = await execute_tool("billing", "get_invoices", {"limit": 100}, c)
    assert not r.ok and not r.blocked and "limit" in r.error
    r = await execute_tool("billing", "get_invoices", {"limit": 3, "bogus": 1}, c)
    assert not r.ok and "bogus" in r.error
    r = await execute_tool("billing", "get_payment_status", "not json", c)
    assert not r.ok and "JSON" in r.error
    r = await execute_tool("billing", "nope", {}, c)
    assert not r.ok and "unknown tool" in r.error

async def test_tool_call_budget(db):
    c = ctx(customers_with("double_charge")[0])
    for _ in range(MAX_TOOL_CALLS_PER_QUERY):
        await execute_tool("billing", "get_customer_profile", {}, c)
    r = await execute_tool("billing", "get_customer_profile", {}, c)
    assert r.blocked and "budget" in r.error

async def test_audit_log_written_for_writes_and_blocks(db):
    cid = customers_with("refund_small_ok")[0]
    c = ctx(cid, qid="q-audit")
    await execute_tool("billing", "create_refund_request", {"invoice_id": facts(cid)["invoice_id"], "reason": "customer requested refund"}, c)
    await execute_tool("billing", "assign_to_human", {"queue": "billing", "priority": "low", "reason": "x" * 6, "summary": "y" * 12}, c)
    async with session_scope() as s:
        rows = (await s.execute(select(m.AuditLog).where(m.AuditLog.query_id == "q-audit"))).scalars().all()
    assert {(r.action, r.outcome) for r in rows} >= {("tool:create_refund_request", "ok"), ("tool:assign_to_human", "blocked")}

def test_tool_schemas_exposed_per_agent_only():
    names = lambda a: {t["function"]["name"] for t in openai_tools(a)}
    assert "create_refund_request" in names("billing") and "create_refund_request" not in names("technical")
    assert "assign_to_human" in names("escalation") and "assign_to_human" not in names("billing")
    assert not any("customer_id" in json.dumps(t) for t in openai_tools("billing"))   # model is never offered an identity param


# ---------- business logic vs ground-truth manifest ----------
async def elig(cid, agent="billing"):
    r = await execute_tool(agent, "check_refund_eligibility", {"invoice_id": facts(cid)["invoice_id"]}, ctx(cid, agent))
    assert r.ok, r.error
    return r.data

async def test_refund_double_charge_is_eligible_duplicate(db):
    for cid in customers_with("double_charge"):
        d = await elig(cid)
        assert d["eligible"] and d["reason_code"] == "duplicate_charge" and d["invoice_id"] == facts(cid)["duplicate_invoice_id"]
        assert d["refundable_usd"] == facts(cid)["amount"] and not d["requires_approval"]
        # the ORIGINAL invoice is not the duplicate, but is within window -> normal eligibility
        o = await execute_tool("billing", "check_refund_eligibility", {"invoice_id": facts(cid)["original_invoice_id"]}, ctx(cid))
        assert o.data["reason_code"] == "within_window"

async def test_refund_annual_outside_window(db):
    for cid in customers_with("annual_outside_window"):
        d = await elig(cid)
        assert not d["eligible"] and d["reason_code"] == "outside_window" and d["window_days"] == 14

async def test_refund_already_refunded_and_failed(db):
    for cid in customers_with("already_refunded"):
        assert (await elig(cid))["reason_code"] == "already_refunded"
    for cid in customers_with("failed_payment"):
        assert (await elig(cid))["reason_code"] == "no_successful_payment"

async def test_refund_over_limit_requires_approval_and_flags_context(db):
    for tag in ("refund_needs_approval", "vip_refund"):
        cid = customers_with(tag)[0]
        d = await elig(cid)
        assert d["eligible"] and d["requires_approval"]
        c = ctx(cid)
        r = await execute_tool("billing", "create_refund_request", {"invoice_id": facts(cid)["invoice_id"], "reason": "wants money back"}, c)
        assert r.ok and r.data["status"] == "pending_approval" and r.requires_human and c.flags["requires_human_approval"]

async def test_refund_small_auto_approved_idempotent_and_ineligible_rejected(db):
    cid = customers_with("refund_small_ok")[0]
    c = ctx(cid)
    args = {"invoice_id": facts(cid)["invoice_id"], "reason": "no longer needed"}
    r1 = await execute_tool("billing", "create_refund_request", args, c)
    assert r1.ok and r1.data["status"] == "approved" and not r1.requires_human
    r2 = await execute_tool("billing", "create_refund_request", args, c)
    assert r2.data["refund_id"] == r1.data["refund_id"]
    async with session_scope() as s:
        assert len((await s.execute(select(m.RefundRequest).where(m.RefundRequest.customer_id == cid))).scalars().all()) == 1
    bad = customers_with("annual_outside_window")[0]
    r3 = await execute_tool("billing", "create_refund_request", {"invoice_id": facts(bad)["invoice_id"], "reason": "please refund"}, ctx(bad))
    assert not r3.ok and "not eligible" in r3.error

async def test_get_invoices_and_failed_payment_details(db):
    cid = customers_with("failed_payment")[0]
    r = await execute_tool("billing", "get_invoices", {"limit": 1}, ctx(cid))
    inv = r.data["invoices"][0]
    assert inv["status"] == "failed" and inv["failure_reason"] == facts(cid)["failure_reason"] and inv["amount"] == facts(cid)["amount"]

async def test_profile_flags_expired_card(db):
    for cid in customers_with("expired_card"):
        r = await execute_tool("billing", "get_customer_profile", {}, ctx(cid))
        assert r.data["card_expired"] is True
    r = await execute_tool("billing", "get_customer_profile", {}, ctx(customers_with("refund_small_ok")[0]))
    assert r.data["card_expired"] is False

async def test_technical_tools(db, kb_bm25):
    cid = customers_with("webhook_timeouts")[0]
    c = ctx(cid, "technical")
    logs = await execute_tool("technical", "get_user_logs", {"window_hours": 168}, c)
    assert logs.data["by_code"][0]["code"] == "WEBHOOK_TIMEOUT"
    diag = await execute_tool("technical", "run_diagnostic", {"check": "webhook_endpoint"}, c)
    assert diag.data["healthy"] is False
    st = await execute_tool("technical", "get_service_status", {}, c)
    assert any(x["component"] == "Webhooks" and x["status"] == "degraded" for x in st.data["components"])
    kb = await execute_tool("technical", "search_knowledge_base", {"query": "webhook timeout 10 seconds retries"}, c)
    assert kb.data["results"][0]["source"].startswith("tech-webhooks")
    t1 = await execute_tool("technical", "create_ticket", {"summary": "Webhook endpoint timeouts since yesterday", "severity": "high"}, c)
    t2 = await execute_tool("technical", "create_ticket", {"summary": "Webhook endpoint timeouts since yesterday", "severity": "high"}, c)
    assert t1.data["ticket_id"] == t2.data["ticket_id"]

async def test_escalation_tools_idempotent_and_eta_by_tier(db):
    vip = customers_with("vip_refund")[0]
    c = ctx(vip, "escalation", qid="q-esc")
    a = {"queue": "escalations", "priority": "high", "reason": "customer very upset", "summary": "Customer upset about repeated billing problems"}
    r1 = await execute_tool("escalation", "assign_to_human", a, c)
    r2 = await execute_tool("escalation", "assign_to_human", {**a, "priority": "critical"}, c)
    assert r1.data["review_id"] == r2.data["review_id"] and r1.data["eta"] == "within 1 hour"
    hist = await execute_tool("escalation", "get_ticket_history", {}, ctx(customers_with("repeat_contact")[0], "escalation"))
    assert hist.data["open_or_escalated"] >= 2


async def test_refund_write_is_intent_gated_on_customer_message(db):
    cid = customers_with("refund_small_ok")[0]
    args = {"invoice_id": facts(cid)["invoice_id"], "reason": "model decided to"}
    c = ToolContext(customer_id=cid, query_id="q-gate", agent="billing", message="why was I charged $49 this month?")
    r = await execute_tool("billing", "create_refund_request", args, c)
    assert r.blocked and "did not ask" in r.error and len(await events("unrequested_write_blocked")) == 1
    async with session_scope() as s:
        assert (await s.execute(select(m.RefundRequest).where(m.RefundRequest.customer_id == cid))).scalars().first() is None
    c2 = ToolContext(customer_id=cid, query_id="q-gate2", agent="billing", message="please give me my money back")
    assert (await execute_tool("billing", "create_refund_request", args, c2)).ok


def test_uncertainty_detector():
    from app.agents.react import UNCERTAIN
    for t in ["The knowledge base does not mention Kafka connectors specifically.", "I'm not certain about that.", "I couldn't find anything about it.",
              "I don't have enough information to confirm that.", "Our documentation doesn't cover that feature."]:
        assert UNCERTAIN.search(t), t
    for t in ["Your webhooks are timing out because your endpoint takes more than 10 seconds.", "You can export up to 2 GB per request.",
              "Refunds are available within 30 days."]:
        assert not UNCERTAIN.search(t), t


async def test_ticket_write_is_intent_gated(db):
    cid = customers_with("webhook_timeouts")[0]
    a = {"summary": "Webhook endpoint timeouts since yesterday", "severity": "high"}
    c = ToolContext(customer_id=cid, query_id="q-tk", agent="technical", message="my webhooks stopped working")
    assert (await execute_tool("technical", "create_ticket", a, c)).blocked
    c2 = ToolContext(customer_id=cid, query_id="q-tk2", agent="technical", message="please open a ticket for my webhook problem")
    assert (await execute_tool("technical", "create_ticket", a, c2)).ok


async def test_concurrent_writes_get_unique_ids_no_failures(db):
    """Regression: count(*)+1 id allocation collided under concurrency (duplicate HRQ/TCK/REF ids -> 'tool failed internally')."""
    import asyncio
    custs = customers_with("repeat_contact")[:6] + customers_with("webhook_timeouts")[:4]
    async def esc(i, cid):
        c = ToolContext(customer_id=cid, query_id=f"q-conc-{i}", agent="escalation")
        return await execute_tool("escalation", "assign_to_human", {"queue": "escalations", "priority": "high", "reason": "concurrency test", "summary": "concurrent escalation test summary"}, c)
    res = await asyncio.gather(*[esc(i, custs[i % len(custs)]) for i in range(30)])
    assert all(r.ok for r in res), [r.error for r in res if not r.ok]
    assert len({r.data["review_id"] for r in res}) == 30
    async def tk(i, cid):
        c = ToolContext(customer_id=cid, query_id=f"q-tk-{i}", agent="technical", message="please open a ticket")
        return await execute_tool("technical", "create_ticket", {"summary": f"Concurrent ticket number {i} for testing", "severity": "low"}, c)
    res = await asyncio.gather(*[tk(i, custs[i % len(custs)]) for i in range(30)])
    assert all(r.ok for r in res) and len({r.data["ticket_id"] for r in res}) == 30


async def test_refund_gate_also_requires_dispatcher_refund_requested(db):
    cid = customers_with("refund_small_ok")[3]
    args = {"invoice_id": facts(cid)["invoice_id"], "reason": "policy question"}
    c = ToolContext(customer_id=cid, query_id="q-rr", agent="billing", message="what is your refund policy for my last invoice?", refund_requested=False)
    assert (await execute_tool("billing", "create_refund_request", args, c)).blocked
    c2 = ToolContext(customer_id=cid, query_id="q-rr2", agent="billing", message="please refund my last invoice", refund_requested=True)
    assert (await execute_tool("billing", "create_refund_request", args, c2)).ok


async def test_kb_category_is_a_soft_preference_not_a_filter(kb_bm25):
    """Regression: the dispatcher labelled an iOS-support question 'general'; a hard category filter then hid the technical article."""
    hits = await kb_bm25.search("what ios version does the mobile app support android crashes", category="general", k=2)
    assert hits and hits[0].doc_id == "tech-mobile-app"
    # ...while an in-category article still wins a tie
    hits = await kb_bm25.search("refund policy window", category="billing", k=1)
    assert hits and hits[0].category in ("billing", "policy")


async def test_review_queue_never_stores_card_numbers_even_if_llm_summary_quotes_them(db):
    c = ToolContext(customer_id=customers_with("repeat_contact")[0], query_id="q-pan", agent="escalation")
    r = await execute_tool("escalation", "assign_to_human", {"queue": "billing", "priority": "high", "reason": "card 4111 1111 1111 1111 disputed",
                                                           "summary": "Customer pasted card 4111 1111 1111 1111 and asks about a charge.", "customer_message": "my card 4111 1111 1111 1111"}, c)
    assert r.ok
    async with session_scope() as s:
        row = (await s.execute(select(m.HumanReview).where(m.HumanReview.id == r.data["review_id"]))).scalar_one()
    assert "4111 1111" not in (row.summary + row.customer_message + row.reason) and "<CARD-1111>" in row.summary
