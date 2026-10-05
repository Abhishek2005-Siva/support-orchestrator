from app.agents.rules import escalation_triggers


def test_repeat_contact_needs_a_complaint_signal_not_just_open_tickets():
    assert "repeat_contact" not in escalation_triggers("What are your support hours?", sentiment="neutral", open_tickets=4)
    assert "repeat_contact" in escalation_triggers("Still not fixed, third time I write", sentiment="neutral", open_tickets=4)
    assert "repeat_contact" in escalation_triggers("My transfer is missing", sentiment="negative", open_tickets=3)


def test_bank_escalation_triggers():
    cases = {"I want to file a formal complaint about this": "regulator_or_formal_complaint", "I will contact my lawyer": "legal_threat",
             "my online banking was hacked and money left": "account_takeover_or_scam", "someone got into my account": "account_takeover_or_scam",
             "I gave the caller my PIN": "account_takeover_or_scam", "my husband passed away last week": "bereavement",
             "why is my wire under review?": "sensitive_aml_topic", "you closed my account": "account_restriction", "I want to speak to a human": "explicit_human_request",
             "you rejected my dispute, I want to appeal the decision": "decision_appeal"}
    for msg, trig in cases.items():
        assert trig in escalation_triggers(msg), msg


def test_fraud_and_card_problems_are_handled_by_agents_not_auto_escalated():
    for msg in ["I don't recognise this payment", "I lost my card", "I was charged twice", "my card was stolen", "there is a fraudulent charge on my card", "I need a dispute for this charge"]:
        assert escalation_triggers(msg) == [], msg


def test_private_segment_unhappy_gets_priority():
    from app.agents.rules import priority_floor
    r = escalation_triggers("this is annoying", sentiment="negative", tier="private")
    assert "vip_unhappy" in r and priority_floor(r, "private") == "high"
    assert priority_floor(["account_takeover_or_scam"], "standard") == "critical"


def test_dispatch_sub_questions_validated_and_pruned():
    from app.schemas.models import DispatchDecision
    from app.agents.dispatcher import apply_overlay
    d = DispatchDecision(intents=["payments", "cards"], confidence=0.9, sub_questions={"payments": "Where is my transfer?", "cards": "Can I raise my limit?", "bogus": "x", "general": 5})
    assert d.sub_questions == {"payments": "Where is my transfer?", "cards": "Can I raise my limit?"}
    out = apply_overlay(d, "Where is my transfer, and can I raise my limit?", {"segment": "standard"})
    assert out.intents == ["payments", "cards"] and set(out.sub_questions) == {"payments", "cards"}
    single = apply_overlay(DispatchDecision(intents=["payments"], confidence=0.9, sub_questions={"payments": "x"}), "Where is my transfer?", None)
    assert single.sub_questions == {}
    both = apply_overlay(DispatchDecision(intents=["cards", "general"], confidence=0.9), "My card is blocked; what are your support hours?", None)
    assert both.intents == ["cards", "general"]      # 'general' is no longer swallowed by a concrete specialist


import pytest
from app.agents.rules import ACTION_REQUEST_RX, BLOCK_RX, CANCEL_RX, DISPUTE_RX, FEE_RX, REPLACE_RX, UNAUTH_RX


@pytest.mark.parametrize("msg", ["Please get my money back for the duplicate.", "I want this charge reversed", "I'd like to dispute the payment", "Can you refund the second charge?",
                                 "please remove the duplicate charge", "I don't recognise this payment", "that payment wasn't me", "give me my money back"])
def test_action_request_phrasings_detected(msg):
    assert ACTION_REQUEST_RX.search(msg)


@pytest.mark.parametrize("msg", ["Why was I charged twice this month?", "What are your support hours?", "Where is my transfer?", "Why was there a fee?"])
def test_non_requests_not_detected(msg):
    assert not ACTION_REQUEST_RX.search(msg)


def test_per_tool_gates():
    assert FEE_RX.search("can you waive the overdraft fee") and not FEE_RX.search("why was I charged an overdraft fee?")
    assert CANCEL_RX.search("please cancel my transfer") and not CANCEL_RX.search("where is my transfer")
    assert BLOCK_RX.search("I lost my card") and BLOCK_RX.search("someone stole my wallet") and not BLOCK_RX.search("how do I change my PIN")
    assert REPLACE_RX.search("I need a replacement card") and not REPLACE_RX.search("block my card")
    assert UNAUTH_RX.search("I didn't make this payment") and not UNAUTH_RX.search("what is my balance")
