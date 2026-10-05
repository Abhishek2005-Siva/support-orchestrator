import asyncio
import json

import pytest
from sqlalchemy import select

from app.db import models as m
from app.db.session import session_scope
from app.tools import handlers  # noqa: registers tools
from app.tools.runtime import MAX_TOOL_CALLS_PER_QUERY, ToolContext, execute_tool, openai_tools
from tests.conftest import MANIFEST, customers_with


def ctx(cid, agent="payments", qid="q-test", msg="", action=None):
    return ToolContext(customer_id=cid, query_id=qid, agent=agent, message=msg, action_requested=action)


def facts(cid): return MANIFEST["customers"][cid]["facts"]


async def events(kind=None):
    async with session_scope() as s:
        q = select(m.SecurityEvent)
        if kind:
            q = q.where(m.SecurityEvent.kind == kind)
        return (await s.execute(q)).scalars().all()


async def verify(cid, issue, sid, agent="payments"):
    r = await execute_tool(agent, "verify_transaction_issue", {"issue_type": issue, "subject_id": sid}, ctx(cid, agent))
    assert r.ok, r.error
    return r.data


# ---------- guardrails (generic) ----------
async def test_allow_list_blocks_and_logs(db):
    c = ctx(customers_with("dup_posted")[0], agent="payments")
    r = await execute_tool("payments", "block_card", {"card_id": "CARD-0000001"}, c)
    assert r.blocked and not r.ok
    r = await execute_tool("payments", "assign_to_human", {"queue": "payments", "priority": "low", "reason": "x" * 6, "summary": "y" * 12}, c)
    assert r.blocked
    assert len(await events("tool_not_allowed")) == 2


async def test_model_cannot_choose_customer(db):
    me, other = customers_with("dup_posted")[0], customers_with("dup_hold")[0]
    r = await execute_tool("payments", "get_transactions", {"limit": 3, "customer_id": other}, ctx(me))
    assert r.blocked and len(await events("authz_violation")) == 1
    r = await execute_tool("payments", "get_transactions", {"limit": 3, "customer_id": me}, ctx(me))
    assert r.ok and r.data["count"] == 3


async def test_cannot_read_other_customers_transaction(db):
    me, other = customers_with("dup_posted")[0], customers_with("dup_hold")[0]
    other_txn = facts(other)["posted_txn_id"]
    r = await execute_tool("payments", "get_transaction_detail", {"txn_id": other_txn}, ctx(me))
    assert not r.ok and "no such record" in r.error
    r2 = await execute_tool("payments", "get_transaction_detail", {"txn_id": "TXN-99999999"}, ctx(me))
    assert r2.error == r.error      # identical message: no enumeration oracle
    v = await execute_tool("payments", "verify_transaction_issue", {"issue_type": "duplicate_charge", "subject_id": other_txn}, ctx(me))
    assert not v.ok and "no such transaction" in v.error


@pytest.mark.parametrize("payload", ["TXN-00000001' OR '1'='1", "TXN-00000001; DROP TABLE transactions", "TXN-1 UNION SELECT 1", "%27%20OR%201%3D1"])
async def test_sqli_in_identifier_blocked_before_db(db, payload):
    r = await execute_tool("payments", "get_transaction_detail", {"txn_id": payload}, ctx(customers_with("dup_posted")[0]))
    assert r.blocked and not r.ok
    assert len(await events("sqli_attempt")) == 1
    async with session_scope() as s:   # tables intact
        assert (await s.execute(select(m.Transaction).limit(1))).scalars().first() is not None


async def test_sqli_in_freetext_blocked_but_benign_sql_words_pass(db, kb_bm25):
    c = ctx(customers_with("dup_posted")[0], agent="cards")
    r = await execute_tool("cards", "search_knowledge_base", {"query": "x' UNION SELECT password FROM users --"}, c)
    assert r.blocked
    r = await execute_tool("cards", "search_knowledge_base", {"query": "how do I select a new PIN and drop my old card"}, c)
    assert r.ok


async def test_invalid_args_return_repairable_error(db):
    c = ctx(customers_with("dup_posted")[0])
    r = await execute_tool("payments", "get_transactions", {"limit": 100}, c)
    assert not r.ok and not r.blocked and "limit" in r.error
    r = await execute_tool("payments", "get_transactions", {"limit": 3, "bogus": 1}, c)
    assert not r.ok and "bogus" in r.error
    r = await execute_tool("payments", "get_transaction_detail", "not json", c)
    assert not r.ok and "JSON" in r.error
    r = await execute_tool("payments", "nope", {}, c)
    assert not r.ok and "unknown tool" in r.error


async def test_tool_call_budget(db):
    c = ctx(customers_with("dup_posted")[0])
    for _ in range(MAX_TOOL_CALLS_PER_QUERY):
        await execute_tool("payments", "get_customer_profile", {}, c)
    r = await execute_tool("payments", "get_customer_profile", {}, c)
    assert r.blocked and "budget" in r.error


def test_tool_schemas_exposed_per_agent_only():
    names = lambda a: {t["function"]["name"] for t in openai_tools(a)}  # noqa: E731
    assert "file_dispute" in names("payments") and "block_card" not in names("payments")
    assert "block_card" in names("cards") and "reverse_fee" not in names("cards")
    assert "assign_to_human" in names("escalation") and "assign_to_human" not in names("payments")
    assert names("general") == {"search_knowledge_base", "get_customer_profile", "get_policy"}
    for a in ("payments", "cards", "general", "escalation"):
        assert not any("customer_id" in json.dumps(t) for t in openai_tools(a))   # the model is never offered an identity param


async def test_profile_and_policy_never_expose_internal_fields(db):
    cid = customers_with("aml_dup")[0]
    r = await execute_tool("payments", "get_customer_profile", {}, ctx(cid))
    assert r.ok and "risk" not in json.dumps(r.data) and "aml" not in json.dumps(r.data).lower()
    r = await execute_tool("payments", "get_policy", {"topic": "AML"}, ctx(cid))
    assert r.ok and r.data["policies"] == []            # compliance rules are internal
    r = await execute_tool("payments", "get_policy", {"topic": "dispute"}, ctx(cid))
    assert r.ok and any(p["policy"] == "POL-DSP-01" and p["params"]["window_days"] == 60 for p in r.data["policies"])
    alerts = await execute_tool("cards", "get_fraud_alerts", {}, ctx(customers_with("unrec_fraud")[0], "cards"))
    assert alerts.ok and "score" not in json.dumps(alerts.data)


# ---------- verification vs ground-truth manifest ----------
@pytest.mark.parametrize("tag,issue,key,decision,reason", [
    ("dup_posted", "duplicate_charge", "target_txn_id", "act", "duplicate_confirmed"),
    ("dup_hold", "duplicate_charge", "pending_txn_id", "wait", "pending_hold"),
    ("dup_legit", "duplicate_charge", None, "no_action", "no_duplicate"),
    ("dup_credited", "duplicate_charge", "target_txn_id", "deny", "already_disputed"),
    ("aml_dup", "duplicate_charge", "target_txn_id", "human", "specialist_review"),
    ("transfer_pending", "transfer_trace", "transfer_id", "wait", "in_transit"),
    ("transfer_overdue", "transfer_trace", "transfer_id", "human", "overdue_trace"),
    ("transfer_returned", "transfer_trace", "transfer_id", "no_action", "returned_to_account"),
    ("wire_done", "transfer_trace", "transfer_id", "no_action", "completed"),
    ("aml_wire", "transfer_trace", "transfer_id", "human", "specialist_review"),
    ("cancel_ok", "transfer_cancel", "transfer_id", "act", "cancel_allowed"),
    ("cancel_wire", "transfer_cancel", "transfer_id", "deny", "not_cancellable"),
    ("cancel_done", "transfer_cancel", "transfer_id", "deny", "not_cancellable"),
    ("fee_waivable", "fee_dispute", "fee_txn_id", "act", "waiver_allowed"),
    ("fee_over_limit", "fee_dispute", "fee_txn_id", "human", "over_limit"),
    ("fee_waiver_used", "fee_dispute", "fee_txn_id", "deny", "waiver_used"),
    ("declined", "declined_payment", "target_txn_id", "no_action", "declined"),
])
async def test_verification_matches_ground_truth(db, tag, issue, key, decision, reason):
    cid = customers_with(tag)[0]
    f = facts(cid)
    sid = f[key] if key else f["txn_ids"][1]
    d = await verify(cid, issue, sid)
    assert (d["decision"], d["reason_code"]) == (decision, reason), d
    assert d["verification_id"].startswith("VER-") and d["consulted"]["database_tables"] and d["consulted"]["knowledge_graph_paths"]
    assert "aml" not in json.dumps(d).lower() and "risk" not in json.dumps(d).lower()     # internal facts never reach the model


@pytest.mark.parametrize("tag,approval", [("dup_posted", "auto"), ("dup_large", "required"), ("dup_kyc_pending", "required"), ("dup_new_account", "required")])
async def test_duplicate_provisional_credit_conditions(db, tag, approval):
    cid = customers_with(tag)[0]
    d = await verify(cid, "duplicate_charge", facts(cid)["target_txn_id"])
    assert d["decision"] == "act" and d["approval"] == approval and d["allowed_actions"] == ["file_dispute"]


async def test_fraud_signals_allow_block_dispute_replace_but_known_merchant_does_not(db):
    cid = customers_with("unrec_fraud")[0]
    d = await verify(cid, "unrecognised_payment", facts(cid)["target_txn_id"], "cards")
    assert d["decision"] == "act" and d["reason_code"] == "fraud_signals" and d["allowed_actions"] == ["block_card", "file_dispute", "request_replacement_card"]
    assert {c["check"]: c["result"] for c in d["checks"]}["geo_mismatch"] == "flag" and d["block_card"] is True
    cid = customers_with("unrec_recurring")[0]
    d = await verify(cid, "unrecognised_payment", facts(cid)["target_txn_id"], "cards")
    assert d["decision"] == "no_action" and d["reason_code"] == "known_merchant"
    cid = customers_with("unrec_plain")[0]
    d = await verify(cid, "unrecognised_payment", facts(cid)["target_txn_id"], "cards")
    assert d["decision"] == "act" and d["reason_code"] == "dispute_allowed" and d["block_card"] is False


async def test_policy_table_drives_the_decision(db):
    """Change a policy row in the database and the same case flips: policy is data, not code."""
    cid = customers_with("dup_large")[0]
    assert (await verify(cid, "duplicate_charge", facts(cid)["target_txn_id"]))["approval"] == "required"
    async with session_scope() as s:
        p = await s.get(m.Policy, "POL-DSP-02")
        p.params = json.dumps({"auto_limit_usd": 5000, "min_account_age_days": 30})
    assert (await verify(cid, "duplicate_charge", facts(cid)["target_txn_id"]))["approval"] == "auto"


async def test_knowledge_graph_drives_which_checks_run(db):
    cid = customers_with("dup_posted")[0]
    d = await verify(cid, "duplicate_charge", facts(cid)["target_txn_id"])
    assert "dispute_window" in [c["check"] for c in d["checks"]]
    async with session_scope() as s:  # remove the window check from the graph: the verifier no longer runs it
        for e in (await s.execute(select(m.KgEdge).where(m.KgEdge.src == "issue:duplicate_charge", m.KgEdge.dst == "chk:dispute_window"))).scalars():
            await s.delete(e)
    d2 = await verify(cid, "duplicate_charge", facts(cid)["target_txn_id"])
    assert "dispute_window" not in [c["check"] for c in d2["checks"]]
    r = await execute_tool("payments", "query_knowledge_graph", {"entity": "duplicate_charge"}, ctx(cid))
    assert r.ok and "dispute_window" not in r.data["checks"] and "POL-DUP-01" in r.data["policies"] and "Reg E (EFTA) §1005.11" in r.data["regulations"]


# ---------- action tools: verify -> act ----------
async def act_ctx(cid, agent, msg, qid="q-act"):
    return ToolContext(customer_id=cid, query_id=qid, agent=agent, message=msg, action_requested=True)


async def test_file_dispute_requires_verification_and_posts_provisional_credit(db):
    cid = customers_with("dup_posted")[0]; f = facts(cid)
    c = await act_ctx(cid, "payments", "I was charged twice, please get my money back")
    r = await execute_tool("payments", "file_dispute", {"verification_id": "VER-000999"}, c)
    assert not r.ok and "verification" in r.error                                   # no verification, no action
    d = await verify(cid, "duplicate_charge", f["target_txn_id"])
    async with session_scope() as s:
        before = (await s.execute(select(m.Account.balance_cents).join(m.Transaction, m.Transaction.account_id == m.Account.id).where(m.Transaction.id == f["target_txn_id"]))).scalar_one()
    r = await execute_tool("payments", "file_dispute", {"verification_id": d["verification_id"], "note": "charged twice"}, c)
    assert r.ok and r.data["status"] == "provisional_credit_issued" and r.data["amount"] == f["amount"] and not r.requires_human
    async with session_scope() as s:
        after = (await s.execute(select(m.Account.balance_cents).join(m.Transaction, m.Transaction.account_id == m.Account.id).where(m.Transaction.id == f["target_txn_id"]))).scalar_one()
        pc = (await s.execute(select(m.Transaction).where(m.Transaction.id == r.data["provisional_credit_txn"]))).scalar_one()
    assert after - before == pc.amount_cents and pc.kind == "provisional_credit" and pc.linked_txn_id == f["target_txn_id"]
    r2 = await execute_tool("payments", "file_dispute", {"verification_id": d["verification_id"]}, await act_ctx(cid, "payments", "please dispute it", "q-act2"))
    assert not r2.ok or r2.data.get("note")                                         # idempotent / the decision no longer allows it again
    d2 = await verify(cid, "duplicate_charge", f["target_txn_id"])
    assert d2["decision"] == "deny" and d2["reason_code"] == "already_disputed"


async def test_large_dispute_is_pending_approval_without_credit(db):
    cid = customers_with("dup_large")[0]; f = facts(cid)
    d = await verify(cid, "duplicate_charge", f["target_txn_id"])
    r = await execute_tool("payments", "file_dispute", {"verification_id": d["verification_id"]}, await act_ctx(cid, "payments", "refund the duplicate please"))
    assert r.ok and r.data["status"] == "pending_approval" and r.requires_human and "provisional_credit_txn" not in r.data


async def test_action_tools_need_the_right_verification_and_decision(db):
    c1, c2 = customers_with("dup_hold")[0], customers_with("fee_waiver_used")[0]
    d = await verify(c1, "duplicate_charge", facts(c1)["pending_txn_id"])             # decision: wait
    r = await execute_tool("payments", "file_dispute", {"verification_id": d["verification_id"]}, await act_ctx(c1, "payments", "dispute the second charge please"))
    assert not r.ok and "does not allow" in r.error
    d2 = await verify(c2, "fee_dispute", facts(c2)["fee_txn_id"])                    # decision: deny
    r = await execute_tool("payments", "reverse_fee", {"verification_id": d2["verification_id"]}, await act_ctx(c2, "payments", "please waive this fee"))
    assert not r.ok
    # a verification of someone else cannot be used
    other = customers_with("fee_waivable")[0]
    d3 = await verify(other, "fee_dispute", facts(other)["fee_txn_id"])
    r = await execute_tool("payments", "reverse_fee", {"verification_id": d3["verification_id"]}, await act_ctx(c2, "payments", "please waive this fee"))
    assert not r.ok and "no such verification" in r.error


async def test_reverse_fee_cancel_transfer_block_and_replace(db):
    cid = customers_with("fee_waivable")[0]; f = facts(cid)
    d = await verify(cid, "fee_dispute", f["fee_txn_id"])
    r = await execute_tool("payments", "reverse_fee", {"verification_id": d["verification_id"]}, await act_ctx(cid, "payments", "please waive the fee"))
    assert r.ok and r.data["amount"] == f["amount"]
    assert (await verify(cid, "fee_dispute", f["fee_txn_id"]))["decision"] == "deny"   # the waiver is now used
    cid = customers_with("cancel_ok")[0]; f = facts(cid)
    d = await verify(cid, "transfer_cancel", f["transfer_id"])
    r = await execute_tool("payments", "cancel_transfer", {"verification_id": d["verification_id"]}, await act_ctx(cid, "payments", "please cancel the transfer"))
    assert r.ok and r.data["status"] == "cancelled"
    async with session_scope() as s:
        t = await s.get(m.Transfer, f["transfer_id"]); tx = await s.get(m.Transaction, t.txn_id)
    assert t.status == "cancelled" and tx.status == "reversed"
    cid = customers_with("lost_card")[0]; f = facts(cid)
    cc = await act_ctx(cid, "cards", "I lost my card, block it and send a replacement")
    r = await execute_tool("cards", "request_replacement_card", {"card_id": f["card_id"]}, cc)
    assert not r.ok and "only issued" in r.error                                   # still active
    r = await execute_tool("cards", "block_card", {"card_id": f["card_id"], "reason": "lost"}, cc)
    assert r.ok and r.data["status"] == "lost"
    assert (await execute_tool("cards", "block_card", {"card_id": f["card_id"], "reason": "lost"}, cc)).data["note"] == "already blocked"
    r = await execute_tool("cards", "request_replacement_card", {"card_id": f["card_id"]}, cc)
    assert r.ok and r.data["status"] == "pending_activation" and r.data["new_card_id"] != f["card_id"]
    again = await execute_tool("cards", "request_replacement_card", {"card_id": f["card_id"]}, cc)
    assert again.data["new_card_id"] == r.data["new_card_id"]                        # idempotent


@pytest.mark.parametrize("tool,args,msg", [
    ("block_card", {"card_id": "CARD-0000001"}, "what is my balance"),
    ("request_replacement_card", {"card_id": "CARD-0000001"}, "I lost my card"),
    ("file_dispute", {"verification_id": "VER-000001"}, "why was I charged a fee?"),
    ("reverse_fee", {"verification_id": "VER-000001"}, "what fees do you charge"),
    ("cancel_transfer", {"verification_id": "VER-000001"}, "where is my transfer"),
])
async def test_writes_are_intent_gated_on_the_customers_own_words(db, tool, args, msg):
    cid = customers_with("lost_card")[0]
    agent = "cards" if tool in ("block_card", "request_replacement_card") else "payments"
    r = await execute_tool(agent, tool, args, ctx(cid, agent, msg=msg))
    assert r.blocked and "did not" in r.error
    assert len(await events("unrequested_write_blocked")) == 1


async def test_dispute_gate_also_needs_dispatchers_action_requested(db):
    cid = customers_with("dup_posted")[0]
    d = await verify(cid, "duplicate_charge", facts(cid)["target_txn_id"])
    a = {"verification_id": d["verification_id"]}
    r = await execute_tool("payments", "file_dispute", a, ToolContext(customer_id=cid, query_id="q1", agent="payments", message="how long does a dispute for a duplicate refund take?", action_requested=False))
    assert r.blocked
    r = await execute_tool("payments", "file_dispute", a, ToolContext(customer_id=cid, query_id="q2", agent="payments", message="please refund the duplicate", action_requested=True))
    assert r.ok


async def test_audit_log_written_for_writes_and_blocks(db):
    cid = customers_with("fee_waivable")[0]
    d = await verify(cid, "fee_dispute", facts(cid)["fee_txn_id"])
    c = ctx(cid, qid="q-audit", msg="please waive this fee", action=True)
    await execute_tool("payments", "reverse_fee", {"verification_id": d["verification_id"]}, c)
    await execute_tool("payments", "assign_to_human", {"queue": "payments", "priority": "low", "reason": "x" * 6, "summary": "y" * 12}, c)
    async with session_scope() as s:
        rows = (await s.execute(select(m.AuditLog).where(m.AuditLog.query_id == "q-audit"))).scalars().all()
    assert {(r.action, r.outcome) for r in rows} >= {("tool:reverse_fee", "ok"), ("tool:assign_to_human", "blocked")}


# ---------- misc ----------
async def test_staff_approval_settles_pending_dispute_with_credit(db):
    from app.graph import helpers as H
    cid = customers_with("dup_large")[0]; f = facts(cid)
    d = await verify(cid, "duplicate_charge", f["target_txn_id"])
    r = await execute_tool("payments", "file_dispute", {"verification_id": d["verification_id"]}, await act_ctx(cid, "payments", "refund the duplicate"))
    assert r.data["status"] == "pending_approval"
    done = await H.settle_pending_disputes(cid, True, "staff-alice")
    assert [x.status for x in done] == ["provisional_credit_issued"]
    async with session_scope() as s:
        pc = (await s.execute(select(m.Transaction).where(m.Transaction.kind == "provisional_credit", m.Transaction.linked_txn_id == f["target_txn_id"]))).scalar_one()
    assert pc.status == "posted" and f"${pc.amount_cents / 100:,.2f}" == f["amount"]


def test_uncertainty_detector():
    from app.agents.react import UNCERTAIN
    for t in ["The knowledge base does not mention crypto wallets specifically.", "I'm not certain about that.", "I couldn't find anything about it.",
              "I don't have enough information to confirm that.", "Our documentation doesn't cover that feature."]:
        assert UNCERTAIN.search(t), t
    for t in ["Your transfer is still within the normal 1-3 business days.", "The daily ATM limit is $800.", "Disputes must be raised within 60 days."]:
        assert not UNCERTAIN.search(t), t


async def test_ticket_write_is_intent_gated(db):
    cid = customers_with("repeat_contact")[0]
    a = {"summary": "Follow up on my card replacement delivery", "severity": "high", "category": "cards"}
    assert (await execute_tool("cards", "create_ticket", a, ToolContext(customer_id=cid, query_id="q-tk", agent="cards", message="my card hasn't arrived"))).blocked
    assert (await execute_tool("cards", "create_ticket", a, ToolContext(customer_id=cid, query_id="q-tk2", agent="cards", message="please open a ticket about my card"))).ok


async def test_concurrent_writes_get_unique_ids_no_failures(db):
    """Regression: count(*)+1 id allocation collided under concurrency (duplicate ids -> 'tool failed internally')."""
    custs = customers_with("repeat_contact")[:6] + customers_with("declined")[:4]

    async def esc(i, cid):
        c = ToolContext(customer_id=cid, query_id=f"q-conc-{i}", agent="escalation")
        return await execute_tool("escalation", "assign_to_human", {"queue": "escalations", "priority": "high", "reason": "concurrency test", "summary": "concurrent escalation test summary"}, c)
    res = await asyncio.gather(*[esc(i, custs[i % len(custs)]) for i in range(30)])
    assert all(r.ok for r in res), [r.error for r in res if not r.ok]
    assert len({r.data["review_id"] for r in res}) == 30

    async def tk(i, cid):
        c = ToolContext(customer_id=cid, query_id=f"q-tk-{i}", agent="payments", message="please open a ticket")
        return await execute_tool("payments", "create_ticket", {"summary": f"Concurrent ticket number {i} for testing", "severity": "low"}, c)
    res = await asyncio.gather(*[tk(i, custs[i % len(custs)]) for i in range(30)])
    assert all(r.ok for r in res) and len({r.data["ticket_id"] for r in res}) == 30
    vs = await asyncio.gather(*[execute_tool("payments", "verify_transaction_issue", {"issue_type": "transfer_trace"}, ctx(custs[i % 3], qid=f"q-v-{i}")) for i in range(12)])
    assert len({v.data["verification_id"] for v in vs if v.ok}) == len([v for v in vs if v.ok])


async def test_kb_category_is_a_soft_preference_not_a_filter(kb_bm25):
    """Regression: a mislabelled intent must not hide the right article."""
    hits = await kb_bm25.search("how do I change my PIN after it got blocked", category="general", k=2)
    assert hits and hits[0].doc_id == "change-pin"
    hits = await kb_bm25.search("dispute window days", category="payments", k=1)
    assert hits and hits[0].doc_id == "dispute-a-transaction"


async def test_review_queue_never_stores_card_numbers_even_if_llm_summary_quotes_them(db):
    c = ToolContext(customer_id=customers_with("repeat_contact")[0], query_id="q-pan", agent="escalation")
    r = await execute_tool("escalation", "assign_to_human", {"queue": "payments", "priority": "high", "reason": "card 4111 1111 1111 1111 disputed",
                                                           "summary": "Customer pasted card 4111 1111 1111 1111 and asks about a charge.", "customer_message": "my card 4111 1111 1111 1111"}, c)
    assert r.ok
    async with session_scope() as s:
        row = (await s.execute(select(m.HumanReview).where(m.HumanReview.id == r.data["review_id"]))).scalar_one()
    assert "4111 1111" not in (row.summary + row.customer_message + row.reason) and "<CARD-1111>" in row.summary
