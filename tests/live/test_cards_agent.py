"""LIVE tests (real NVIDIA calls): Cards & fraud agent. Assertions on card status, disputes and replacement cards in the database."""
import re, sqlite3
import pytest
from tests.conftest import customers_with, MANIFEST
from tests.live.test_payments_agent import ask, facts, has, q

pytestmark = pytest.mark.live
async def cask(db, tag, msg, i=0, action=True): return await ask(db, tag, msg, i, agent="cards", action=action)

async def test_lost_card_is_blocked_and_replacement_only_if_asked(db):
    cid, r = await cask(db, "lost_card", "I lost my wallet, please block my card.")
    assert q(db, "select status from cards where id=?", facts(cid)["card_id"]) == [("lost",)] and has(r, r"block"), r.reply
    assert q(db, "select count(*) from cards where customer_id=? and status='pending_activation'", cid)[0][0] == 0
    cid2, r2 = await cask(db, "lost_card_fraud", "My card was stolen, block it and send me a replacement card.")
    assert q(db, "select count(*) from cards where customer_id=? and status='pending_activation'", cid2)[0][0] == 1

async def test_foreign_burst_is_blocked_disputed_and_explained(db):
    cid, r = await cask(db, "unrec_fraud", "I don't recognise the {amount} payment at {merchant}.")
    f = facts(cid)
    assert q(db, "select status from cards where id=?", f["card_id"]) == [("blocked",)]
    assert q(db, "select reason, status from disputes where customer_id=?", cid) == [("unauthorized", "provisional_credit_issued")]
    assert has(r, r"block") and has(r, r"DSP-\d{6}|dispute"), r.reply
    assert not has(r, r"fraud (score|engine)"), "scores are internal"

async def test_known_merchant_is_not_treated_as_fraud(db):
    cid, r = await cask(db, "unrec_recurring", "I don't recognise the {amount} payment at {merchant}, it's not mine.")
    assert q(db, "select count(*) from disputes where customer_id=?", cid)[0][0] == 0
    assert q(db, "select count(*) from cards where customer_id=? and status in ('blocked','lost')", cid)[0][0] == 0
    assert has(r, r"regular|before|previous|earlier|paid|subscription"), r.reply

async def test_unfamiliar_domestic_payment_is_disputed_without_blocking(db):
    cid, r = await cask(db, "unrec_plain", "I don't recognise the {amount} payment at {merchant}, I did not make it.")
    assert q(db, "select reason from disputes where customer_id=?", cid) == [("unauthorized",)]
    assert q(db, "select count(*) from cards where customer_id=? and status in ('blocked','lost')", cid)[0][0] == 0

async def test_decline_reason_is_explained_from_the_ledger(db):
    words = {"insufficient_funds": r"balance|insufficient|funds", "daily_limit": r"limit", "intl_disabled": r"international|abroad", "suspected_fraud": r"fraud", "card_blocked": r"block", "wrong_pin": r"pin"}
    for i in range(3):
        cid, r = await cask(db, "declined", "Why was my card declined at {merchant}?", i=i, action=False)
        assert has(r, words[facts(cid)["reason"]]), (facts(cid)["reason"], r.reply)

async def test_cards_agent_cannot_move_money_other_than_through_a_verified_dispute(db):
    from app.tools.runtime import openai_tools
    names = {t["function"]["name"] for t in openai_tools("cards")}
    assert "reverse_fee" not in names and "cancel_transfer" not in names and "assign_to_human" not in names
