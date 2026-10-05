"""LIVE tests (real NVIDIA calls): Payments agent. Run: pytest -m live tests/live/test_payments_agent.py -v
Assertions are on DATABASE state (disputes, credits, waivers, transfers) as well as on the reply text, against the seeded ground truth."""
import re, sqlite3
import pytest
from app.core.config import ROOT
from tests.conftest import customers_with, MANIFEST

pytestmark = pytest.mark.live
def facts(cid): return MANIFEST["customers"][cid]["facts"]
def has(r, pattern): return re.search(pattern, r.reply, re.I) is not None
def q(db, sql, *a):
    c = sqlite3.connect(db); rows = c.execute(sql, a).fetchall(); c.close(); return rows

async def ask(db, tag, msg, i=0, agent="payments", action=None):
    from app.agents.specialists import run_specialist
    cid = customers_with(tag)[i]
    r = await run_specialist(agent, message=msg.format(**{k: v for k, v in facts(cid).items() if isinstance(v, (str, int))}), customer_id=cid, query_id=f"live-{tag}-{i}",
                             dispatch={"intents": [agent], "action_requested": action} if action is not None else None)
    return cid, r

async def test_duplicate_explained_without_filing_when_not_asked(db):
    cid, r = await ask(db, "dup_posted", "I think I was charged twice at {merchant}.", action=False)
    assert has(r, r"duplicate|twice|two"), r.reply
    assert q(db, "select count(*) from disputes where customer_id=?", cid)[0][0] == 0, "no dispute without a request"

async def test_duplicate_dispute_filed_with_provisional_credit(db):
    cid, r = await ask(db, "dup_posted", "I was charged twice at {merchant}, please get my money back.", action=True)
    d = q(db, "select txn_id, status, reason from disputes where customer_id=?", cid)
    assert d == [(facts(cid)["target_txn_id"], "provisional_credit_issued", "duplicate")], d
    assert facts(cid)["amount"] in r.reply and not r.requires_human_approval
    assert q(db, "select count(*) from transactions where customer_id=? and kind='provisional_credit'", cid)[0][0] == 1

async def test_pending_hold_is_not_a_duplicate(db):
    cid, r = await ask(db, "dup_hold", "I was charged twice at {merchant}, please refund the duplicate.", action=True)
    assert has(r, r"pending|hold|authori[sz]ation"), r.reply
    assert q(db, "select count(*) from disputes where customer_id=?", cid)[0][0] == 0

async def test_large_duplicate_is_pending_for_a_human(db):
    cid, r = await ask(db, "dup_large", "I was charged {amount} twice at {merchant}, please refund the duplicate.", action=True)
    assert r.requires_human_approval and has(r, r"approv"), r.reply
    assert q(db, "select status from disputes where customer_id=?", cid) == [("pending_approval",)]
    assert q(db, "select count(*) from transactions where customer_id=? and kind='provisional_credit'", cid)[0][0] == 0
    assert not has(r, r"credit (of \$[\d,.]+ )?(has been|was) (posted|issued)")

async def test_internal_flag_customer_goes_to_a_human_and_nothing_leaks(db):
    cid, r = await ask(db, "aml_dup", "I was charged twice at {merchant}, please refund the duplicate.", action=True)
    assert r.needs_human, r
    assert not has(r, r"\bAML\b|laundering|compliance|risk flag|internal review|fraud score")
    assert q(db, "select count(*) from disputes where customer_id=?", cid)[0][0] == 0

async def test_transfer_in_transit_explained_not_escalated(db):
    cid, r = await ask(db, "transfer_pending", "My transfer of {amount} to {to_name} has not arrived.", action=False)
    assert has(r, r"business day|expected|normal"), r.reply
    assert not r.needs_human

async def test_returned_transfer_explains_the_return_code(db):
    cid, r = await ask(db, "transfer_returned", "What happened to my {amount} transfer to {to_name}?", action=False)
    assert facts(cid)["return_code"] in r.reply and has(r, r"return"), r.reply
    assert not r.needs_human, "a fact about the receiving bank is not the agent's uncertainty"

async def test_cancel_pending_transfer_and_refuse_a_wire(db):
    cid, r = await ask(db, "cancel_ok", "Please cancel the {amount} transfer I just made.", action=True)
    assert q(db, "select status from transfers where id=?", facts(cid)["transfer_id"]) == [("cancelled",)]
    cid, r = await ask(db, "cancel_wire", "Please cancel the {amount} wire I just sent.", action=True)
    assert q(db, "select status from transfers where id=?", facts(cid)["transfer_id"]) == [("pending",)] and has(r, r"cannot|can't|irrevocable|unable"), r.reply

async def test_fee_waiver_once_and_then_refused(db):
    cid, r = await ask(db, "fee_waivable", "Can you waive the {description} I was charged?", action=True)
    assert q(db, "select count(*) from fee_waivers where customer_id=?", cid)[0][0] == 1
    cid2, r2 = await ask(db, "fee_waiver_used", "Can you waive the {description} I was charged?", action=True)
    assert q(db, "select count(*) from fee_waivers where customer_id=?", cid2)[0][0] == 1 and has(r2, r"already|once|12 months|previous"), r2.reply

async def test_policy_question_uses_the_policy_table(db):
    cid, r = await ask(db, "dup_posted", "How long does a dispute take and what is the dispute window?", action=False)
    assert has(r, r"60 days") and has(r, r"10 business days"), r.reply
    assert q(db, "select count(*) from disputes where customer_id=?", cid)[0][0] == 0
