from app.agents.rules import escalation_triggers


def test_repeat_contact_needs_a_complaint_signal_not_just_open_tickets():
    from app.agents.rules import escalation_triggers
    assert "repeat_contact" not in escalation_triggers("What is your uptime SLA?", sentiment="neutral", open_tickets=4)
    assert "repeat_contact" in escalation_triggers("Still not fixed, third time I write", sentiment="neutral", open_tickets=4)
    assert "repeat_contact" in escalation_triggers("My invoice is wrong", sentiment="negative", open_tickets=3)


def test_dispatch_sub_questions_validated_and_pruned():
    from app.schemas.models import DispatchDecision
    from app.agents.dispatcher import apply_overlay
    d = DispatchDecision(intents=["billing", "technical"], confidence=0.9, sub_questions={"billing": "How do I cancel?", "technical": "Can I export?", "bogus": "x", "general": 5})
    assert d.sub_questions == {"billing": "How do I cancel?", "technical": "Can I export?"}
    out = apply_overlay(d, "How do I cancel, and can I export?", {"tier": "standard"})
    assert out.intents == ["billing", "technical"] and set(out.sub_questions) == {"billing", "technical"}
    single = apply_overlay(DispatchDecision(intents=["billing"], confidence=0.9, sub_questions={"billing": "x"}), "How do I cancel?", None)
    assert single.sub_questions == {}
    both = apply_overlay(DispatchDecision(intents=["technical", "general"], confidence=0.9), "SSO fails; support hours?", None)
    assert both.intents == ["technical", "general"]      # 'general' is no longer swallowed by a concrete specialist


import pytest
from app.agents.rules import REFUND_REQUEST_RX

@pytest.mark.parametrize("msg", ["I'd like to cancel and get the last charge returned.", "Please reverse my last payment, I don't need Orbit anymore.",
                                 "You took my money twice; please give back the extra payment.", "please send back the duplicated payment", "double billing again - I need the duplicate returned to my card",
                                 "Could you reimburse my most recent payment?", "i want to be refunded for the newest invoice", "give me my money back"])
def test_refund_request_phrasings_detected(msg):
    assert REFUND_REQUEST_RX.search(msg)

@pytest.mark.parametrize("msg", ["Why was I charged twice this month?", "How do I download my invoice?", "What are your support hours?", "Is my payment method safe?"])
def test_non_requests_not_detected(msg):
    assert not REFUND_REQUEST_RX.search(msg)
