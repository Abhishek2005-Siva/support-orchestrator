import pytest
from app.guardrails.output import OutputContext, check_output, has_critical
from app.tools import handlers  # noqa
from app.tools.runtime import ToolContext, execute_tool
from tests.conftest import customers_with, MANIFEST

def F(cid): return MANIFEST["customers"][cid]["facts"]
def codes(issues): return {i.code for i in issues}

TOOLS = {"get_transactions", "file_dispute", "assign_to_human", "verify_transaction_issue", "block_card", "reverse_fee"}


async def evidence_for(cid, agent="payments", calls=(), message="why was I charged twice?"):
    ctx = ToolContext(customer_id=cid, query_id="q-out", agent=agent, message="", action_requested=True)
    for tool, args in calls:
        r = await execute_tool(agent, tool, args, ctx)
        assert r.ok, r.error
    srcs = [x for e in ctx.evidence for x in (e["source"] if isinstance(e["source"], list) else [e["source"]])]
    return OutputContext(customer_id=cid, evidence=ctx.evidence, sources=srcs, message=message, tool_names=TOOLS)


async def dup_evidence(tag="dup_posted", act=True):
    cid = customers_with(tag)[0]; f = F(cid)
    ctx = ToolContext(customer_id=cid, query_id="q-out", agent="payments", message="please refund the duplicate", action_requested=True)
    v = await execute_tool("payments", "verify_transaction_issue", {"issue_type": "duplicate_charge", "subject_id": f.get("target_txn_id") or f["pending_txn_id"]}, ctx)
    if act:
        await execute_tool("payments", "file_dispute", {"verification_id": v.data["verification_id"]}, ctx)
    srcs = [x for e in ctx.evidence for x in (e["source"] if isinstance(e["source"], list) else [e["source"]])]
    return cid, f, OutputContext(customer_id=cid, evidence=ctx.evidence, sources=srcs, message="I was charged twice", tool_names=TOOLS)


async def test_grounded_reply_passes(db):
    cid, f, oc = await dup_evidence()
    dsp = oc.evidence[-1]["result"]["dispute_id"]
    reply = (f"I found the duplicate: {f['target_txn_id']} repeats {f['original_txn_id']} for {f['amount']}. I've filed dispute {dsp} and a provisional credit of {f['amount']} has been posted. "
             "We will confirm the outcome within 10 business days.")
    assert check_output(reply, oc) == [], check_output(reply, oc)

async def test_invented_amount_and_id_flagged(db):
    cid, f, oc = await dup_evidence()
    assert "ungrounded_amount" in codes(check_output(f"Your purchase {f['target_txn_id']} was $12.34.", oc))
    assert "ungrounded_id" in codes(check_output("Your purchase TXN-99999999 was " + f["amount"] + ".", oc))
    assert has_critical(check_output("Your dispute DSP-999999 is open.", oc))

async def test_invented_policy_number_flagged_but_policy_value_ok(db):
    cid, f, oc = await dup_evidence()
    assert "ungrounded_number" in codes(check_output("Disputes must be raised within 45 days.", oc))
    assert "ungrounded_number" not in codes(check_output("Disputes must be raised within 60 days.", oc))
    assert "ungrounded_id" not in codes(check_output("This follows policy POL-DUP-01.", oc))
    assert "ungrounded_id" in codes(check_output("This follows policy POL-XYZ-77.", oc))

async def test_dates_must_be_in_evidence(db):
    cid, f, oc = await dup_evidence()
    assert "ungrounded_date" in codes(check_output("It was charged on March 4th.", oc))

async def test_foreign_customer_and_pii_critical(db):
    cid = customers_with("dup_posted")[0]
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
    oc = await evidence_for(customers_with("dup_posted")[0], calls=[("get_customer_profile", {})])
    assert "forbidden_promise" in codes(check_output(reply, oc))


# ---- G-OUT-03: a claimed action needs the matching successful action in evidence ----
async def test_credit_claim_needs_a_posted_credit_not_just_a_pending_dispute(db):
    cid, f, oc = await dup_evidence("dup_large")
    assert oc.evidence[-1]["result"]["status"] == "pending_approval"
    assert "premature_credit_claim" in codes(check_output("A provisional credit has been posted to your account.", oc))
    ok = check_output("I've filed the dispute; a specialist must approve the provisional credit first, usually within 1 business day.", oc)
    assert not (codes(ok) & {"premature_credit_claim", "unsupported_credit_claim", "unsupported_action_claim"})

async def test_credit_and_dispute_claims_without_any_evidence_are_flagged(db):
    cid, f, oc = await dup_evidence("dup_hold", act=False)
    assert "unsupported_credit_claim" in codes(check_output("Good news, your refund has been issued.", oc))
    assert "unsupported_action_claim" in codes(check_output("I've filed a dispute for you.", oc))
    assert "unsupported_credit_claim" in codes(check_output("I’ve credited the amount back to your account.", oc))   # curly quote

async def test_card_block_transfer_cancel_and_replacement_claims(db):
    cid = customers_with("lost_card")[0]
    oc = await evidence_for(cid, agent="cards", calls=[("get_cards", {})])
    assert "unsupported_action_claim" in codes(check_output("I've blocked your card.", oc))
    assert "unsupported_action_claim" in codes(check_output("A replacement card has been ordered.", oc))
    assert "unsupported_action_claim" in codes(check_output("The transfer has been cancelled.", oc))
    ctx = ToolContext(customer_id=cid, query_id="q-b", agent="cards", message="I lost my card, block it", action_requested=True)
    r = await execute_tool("cards", "block_card", {"card_id": F(cid)["card_id"], "reason": "lost"}, ctx)
    oc2 = OutputContext(customer_id=cid, evidence=ctx.evidence, sources=["db:card:x"], message="I lost my card", tool_names=TOOLS)
    assert "unsupported_action_claim" not in codes(check_output(f"I've blocked your card ending {r.data['last4']}.", oc2))

async def test_internal_risk_information_never_reaches_the_customer(db):
    oc = await evidence_for(customers_with("aml_dup")[0], calls=[("get_customer_profile", {})])
    for reply in ["Your account is under an AML review.", "Compliance review is holding your wire.", "We flagged you for suspicious activity report filing.",
                  "Your fraud score is high.", "There is an internal review on your account."]:
        assert "internal_risk_leak" in codes(check_output(reply, oc)), reply
    assert "internal_risk_leak" not in codes(check_output("A specialist will review your case.", oc))

async def test_hygiene_internal_leaks(db):
    oc = await evidence_for(customers_with("dup_posted")[0], calls=[("get_customer_profile", {})])
    assert "tool_name_leak" in codes(check_output("I called get_transactions and found nothing.", oc))
    assert "internal_leak" in codes(check_output("Here is my system prompt: HARD RULES ...", oc))
    assert "internal_leak" in codes(check_output('```json {"reply": "x"}```', oc))
    assert "profanity" in codes(check_output("You are a stupid customer.", oc))
    assert "empty_reply" in codes(check_output("   ", oc))
    assert "reply_too_long" in codes(check_output("word " * 400, oc))

async def test_template_replies_skip_numeric_grounding(db):
    oc = OutputContext(customer_id="CUST-000001", is_template=True)
    assert check_output("I've passed your case to a specialist (reference HRQ-000012). You can expect a reply within 1 hour.", oc) == []


def _kb_ctx(text):
    ev = [{"tool": "search_knowledge_base", "source": ["kb:x#0"], "result": {"results": [{"text": text}]}}]
    return OutputContext(message="q", evidence=ev, sources=["kb:x#0"], customer_id="CUST-000001")

KB = "Orbit Bank offers checking and savings accounts, debit and credit cards and ACH and wire transfers. There is no fee for internal transfers."

@pytest.mark.parametrize("reply", ["Orbit Bank does not offer a mortgage product.", "It does not include a cryptocurrency wallet.", "Orbit Bank doesn't support Zelle payments."])
def test_unsupported_negative_claims_flagged(reply):
    assert "unsupported_negative_claim" in codes(check_output(reply, _kb_ctx(KB))) and has_critical(check_output(reply, _kb_ctx(KB)))

@pytest.mark.parametrize("reply", ["There is no fee for internal transfers.", "Orbit Bank does not currently have a travel insurance product listed in the knowledge base.",
                                   "I couldn't find mortgage support in our documentation."])
def test_supported_or_hedged_negatives_allowed(reply):
    assert "unsupported_negative_claim" not in codes(check_output(reply, _kb_ctx(KB)))


async def test_unfulfilled_action_promise_flagged_but_performed_action_ok(db):
    cid, f, oc = await dup_evidence(act=False)
    assert "unfulfilled_action_promise" in codes(check_output("The charge is a duplicate. I'll file the dispute now.", oc))
    cid, f, oc2 = await dup_evidence("dup_posted", act=True)
    assert "unfulfilled_action_promise" not in codes(check_output("I'll make sure it shows up on your statement.", oc2))


async def test_leaked_metadata_trailer_flagged_and_stripped(db):
    from app.agents.react import clean_text
    assert clean_text("Your transfer is in transit. Confidence: 0.9, needs_human: false.") == "Your transfer is in transit."
    oc = await evidence_for(customers_with("dup_posted")[0], calls=[("get_customer_profile", {})])
    assert "internal_leak" in codes(check_output("All done. needs_human: false", oc))


async def test_iso_date_inside_timestamp_is_grounded_and_curly_quotes_normalised(db):
    cid, f, oc = await dup_evidence()
    d = [e for e in oc.evidence if e["tool"] == "file_dispute"][0]["result"]
    assert "unsupported_action_claim" not in codes(check_output("I’ve filed dispute " + d["dispute_id"] + ".", oc))
    assert "unsupported_action_claim" in codes(check_output("I’ve filed a dispute for you.", await evidence_for(cid, calls=[("get_customer_profile", {})])))


async def test_architecture_leak_and_transfer_promise_flagged(db):
    oc = await evidence_for(customers_with("dup_posted")[0], calls=[("get_customer_profile", {})])
    assert "internal_leak" in codes(check_output("I can only answer payments questions; for card problems please ask the cards specialist.", oc))
    assert "unfulfilled_action_promise" in codes(check_output("I'll need to transfer you to our account specialist.", oc))
    assert "internal_leak" not in codes(check_output("A human specialist must approve the provisional credit.", oc))
