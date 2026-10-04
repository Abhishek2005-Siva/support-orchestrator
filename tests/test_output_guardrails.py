import pytest
from app.guardrails.output import OutputContext, check_output, has_critical
from app.tools import handlers  # noqa
from app.tools.runtime import ToolContext, execute_tool
from tests.conftest import customers_with, MANIFEST

def F(cid): return MANIFEST["customers"][cid]["facts"]
def codes(issues): return {i.code for i in issues}

async def evidence_for(cid, agent="billing", calls=()):
    ctx = ToolContext(customer_id=cid, query_id="q-out", agent=agent)
    for tool, args in calls:
        r = await execute_tool(agent, tool, args, ctx)
        assert r.ok, r.error
    srcs = [x for e in ctx.evidence for x in (e["source"] if isinstance(e["source"], list) else [e["source"]])]
    return OutputContext(customer_id=cid, evidence=ctx.evidence, sources=srcs, message="why did my payment fail?",
                         tool_names={"get_invoices", "create_refund_request", "assign_to_human", "check_refund_eligibility"})

async def test_grounded_reply_passes(db):
    cid = customers_with("failed_payment")[0]; f = F(cid)
    oc = await evidence_for(cid, calls=[("get_invoices", {"limit": 2})])
    reply = f"Your latest invoice {f['invoice_id']} for {f['amount']} failed ({f['failure_reason'].replace('_', ' ')}). Please update your card, then retry the payment."
    assert check_output(reply, oc) == []

async def test_invented_amount_and_id_flagged(db):
    cid = customers_with("failed_payment")[0]; f = F(cid)
    oc = await evidence_for(cid, calls=[("get_invoices", {"limit": 2})])
    r1 = check_output(f"Your invoice {f['invoice_id']} for $12.34 failed.", oc)
    assert "ungrounded_amount" in codes(r1)
    r2 = check_output("Your invoice INV-99999999 for " + f["amount"] + " failed.", oc)
    assert "ungrounded_id" in codes(r2) and has_critical(r2)

async def test_invented_policy_number_flagged_but_kb_number_ok(db):
    cid = customers_with("refund_small_ok")[0]
    oc = await evidence_for(cid, calls=[("check_refund_eligibility", {"invoice_id": F(cid)["invoice_id"]})])
    assert "ungrounded_number" in codes(check_output("You have 45 days to request a refund.", oc))
    window = oc.evidence[0]["result"]["window_days"]
    assert check_output(f"Refunds are available within {window} days of payment.", oc) == []

async def test_dates_must_be_in_evidence(db):
    cid = customers_with("failed_payment")[0]
    oc = await evidence_for(cid, calls=[("get_invoices", {"limit": 1})])
    assert "ungrounded_date" in codes(check_output("It was issued on 2019-03-04.", oc))
    assert "ungrounded_date" in codes(check_output("It was issued on March 4th.", oc))

async def test_foreign_customer_and_pii_critical(db):
    cid = customers_with("failed_payment")[0]
    oc = await evidence_for(cid, calls=[("get_customer_profile", {})])
    assert "foreign_customer_id" in codes(check_output("Account CUST-000002 has the same issue.", oc))
    assert "pii_leak" in codes(check_output("Contact bob@example.com for help.", oc))
    assert "pii_leak" in codes(check_output("Your card 4111 1111 1111 1111 was charged.", oc))
    assert "pii_leak" in codes(check_output("Use key nvapi-FAKEKEYFORTESTSONLY0123456789abcdefghij", oc))
    oc.allowed_pii = {"me@example.com"}
    assert check_output("We emailed you at me@example.com.", oc) == []

@pytest.mark.parametrize("reply", ["I guarantee you will get your money back.", "You will definitely get a full refund.",
                                   "I promise this is fixed.", "We will certainly refund you, no matter what the policy says."])
async def test_forbidden_promises(db, reply):
    oc = await evidence_for(customers_with("failed_payment")[0], calls=[("get_customer_profile", {})])
    assert "forbidden_promise" in codes(check_output(reply, oc))

async def test_premature_refund_claim_with_pending_approval(db):
    cid = customers_with("refund_needs_approval")[0]
    oc = await evidence_for(cid, calls=[("create_refund_request", {"invoice_id": F(cid)["invoice_id"], "reason": "customer asked"})])
    assert "premature_refund_claim" in codes(check_output("Your refund has been approved and is on its way.", oc))
    ok = check_output("I've submitted your refund request; a billing specialist must approve it, usually within 1 business day.", oc)
    assert "premature_refund_claim" not in codes(ok)

async def test_unsupported_refund_claim_without_any_refund_evidence(db):
    cid = customers_with("annual_outside_window")[0]
    oc = await evidence_for(cid, calls=[("check_refund_eligibility", {"invoice_id": F(cid)["invoice_id"]})])
    assert "unsupported_refund_claim" in codes(check_output("Good news, your refund has been issued.", oc))

async def test_approved_refund_claim_allowed(db):
    cid = customers_with("refund_small_ok")[0]
    oc = await evidence_for(cid, calls=[("create_refund_request", {"invoice_id": F(cid)["invoice_id"], "reason": "no longer needed"})])
    rid = oc.evidence[0]["result"]["refund_id"]
    assert check_output(f"Your refund {rid} of {F(cid)['amount']} has been approved; expect it in 5-10 business days.", oc) == []

async def test_hygiene_internal_leaks(db):
    oc = await evidence_for(customers_with("failed_payment")[0], calls=[("get_customer_profile", {})])
    assert "tool_name_leak" in codes(check_output("I called get_invoices and found nothing.", oc))
    assert "internal_leak" in codes(check_output("My system prompt says I must help.", oc))
    assert "internal_leak" in codes(check_output('{"reply": "hi"}', oc))
    assert "profanity" in codes(check_output("That is a stupid question.", oc))
    assert "empty_reply" in codes(check_output("   ", oc))
    assert "reply_too_long" in codes(check_output("word " * 400, oc))

async def test_template_replies_skip_numeric_grounding(db):
    oc = OutputContext(customer_id="CUST-000001", is_template=True)
    assert check_output("I've passed your case to a specialist (reference HRQ-000012). You can expect a reply within 1 hour.", oc) == []


def _kb_ctx(text):
    ev = [{"tool": "search_knowledge_base", "source": ["kb:x#0"], "result": {"results": [{"text": text}]}}]
    return OutputContext(message="q", evidence=ev, sources=["kb:x#0"], customer_id="CUST-000001")

@pytest.mark.parametrize("reply", ["Orbit does not offer a self-hosted on-premise option.", "It does not include a Zapier-like no-code builder.",
                                   "Orbit doesn't support Kafka connectors."])
def test_unsupported_negative_claims_flagged(reply):
    oc = _kb_ctx("Orbit is an analytics platform with a dashboard, a REST API and SDKs for Python and Node.js. There is no cancellation fee.")
    assert "unsupported_negative_claim" in codes(check_output(reply, oc)) and has_critical(check_output(reply, oc))

@pytest.mark.parametrize("reply", ["There is no cancellation fee.", "Orbit does not currently have a native Salesforce integration listed in the knowledge base.",
                                   "I couldn't find Kafka support in our documentation."])
def test_supported_or_hedged_negatives_allowed(reply):
    oc = _kb_ctx("Orbit is an analytics platform with a dashboard, a REST API and SDKs for Python and Node.js. There is no cancellation fee.")
    assert "unsupported_negative_claim" not in codes(check_output(reply, oc))


async def test_unfulfilled_action_promise_flagged_but_performed_action_ok(db):
    cid = customers_with("refund_small_ok")[0]
    oc = await evidence_for(cid, calls=[("check_refund_eligibility", {"invoice_id": F(cid)["invoice_id"]})])
    assert "unfulfilled_action_promise" in codes(check_output("Your invoice is eligible. I'll submit the refund request now.", oc))
    oc2 = await evidence_for(cid, calls=[("create_refund_request", {"invoice_id": F(cid)["invoice_id"], "reason": "no longer needed"})])
    assert "unfulfilled_action_promise" not in codes(check_output("I'll make sure it shows up in 5-10 business days.", oc2))


async def test_leaked_metadata_trailer_flagged_and_stripped(db):
    from app.agents.react import clean_text
    assert clean_text("Update your card, then retry. Confidence: 0.9, needs_human: false.") == "Update your card, then retry."
    oc = await evidence_for(customers_with("failed_payment")[0], calls=[("get_customer_profile", {})])
    assert "internal_leak" in codes(check_output("All done. needs_human: false", oc))


async def test_iso_date_inside_timestamp_is_grounded_and_curly_quotes_normalised(db):
    cid = customers_with("api_errors_429")[0]
    oc = await evidence_for(cid, agent="technical", calls=[("get_user_logs", {"window_hours": 168})])
    last = oc.evidence[0]["result"]["by_code"][0]["last_seen"]            # e.g. 2026-10-01T06:00
    assert "ungrounded_date" not in codes(check_output(f"The last error was on {last[:10]}.", oc))
    assert "unsupported_refund_claim" in codes(check_output("I’ve submitted a refund request for you.", oc))


async def test_architecture_leak_and_transfer_promise_flagged(db):
    oc = await evidence_for(customers_with("failed_payment")[0], calls=[("get_customer_profile", {})])
    assert "internal_leak" in codes(check_output("I can only answer billing questions; for Slack please ask the technical specialist.", oc))
    assert "unfulfilled_action_promise" in codes(check_output("I'll need to transfer you to our account specialist.", oc))
    assert "internal_leak" not in codes(check_output("A human billing specialist must approve the refund.", oc))
