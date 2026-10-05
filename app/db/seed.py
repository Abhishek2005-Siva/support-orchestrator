"""Deterministic simulated bank. `python -m app.db.seed` (re)builds data/support.db.

~300 customers with accounts, cards, a ~15k-row transaction ledger, transfers (ACH / wire / internal) and policies, plus deliberate
scenarios (duplicate charges, fraud, lost cards, stuck transfers, fees, declines, AML flags). Every scenario is recorded in
data/seed_manifest.json with the facts an evaluation needs (transaction ids, amounts, expected decision), so the golden dataset has
ground truth that does not depend on any LLM.

SYNTHETIC DATA ONLY. Names/emails/phones/cards are fabricated (example.com, 555-01xx). Merchant names are fictional.
"""
from __future__ import annotations

import asyncio
import json
import random
import secrets
from collections import defaultdict
from datetime import date, datetime, timedelta

from sqlalchemy import delete

from app.core.config import ROOT
from app.core.security import hash_secret
from app.db import models as m
from app.db.session import init_db, session_scope
from app.tools import kg
from app.tools.rules import DEFAULT_POLICIES, add_business_days

FIRST = "Aiden Bella Carlos Dana Elias Fatima Gabriel Hana Ivan Julia Kenji Lena Mateo Nora Omar Priya Quinn Rosa Samir Tara Umar Vera Wen Xavi Yara Zane Amara Bruno Chloe Dmitri Esme Farid Grace Hugo Ines Jonas Kira Leo Mina Nico Olga Paul Rita Sven Tess Uma Viktor Willa Yusuf Zoe".split()
LAST = "Alvarez Berg Chen Dubois Evans Fischer Garcia Haddad Ito Jensen Khan Lopez Moreau Nowak Okafor Petrov Quist Rossi Silva Tanaka Usman Varga Weber Xu Yilmaz Zhang Abbott Brandt Costa Dahl Egan Fox Gill Hart Ivers Joshi Kerr Lund Marsh Nash Ortiz Pratt Reyes Shaw Tran Voss Wolfe Young Zeller".split()

# (name, mcc, category, country, risk)
MERCHANTS = [
    ("Greenfield Grocers", "5411", "Groceries", "US", "low"), ("FreshMart", "5411", "Groceries", "US", "low"), ("Harvest Table Market", "5411", "Groceries", "US", "low"),
    ("Shell Station 4471", "5541", "Fuel", "US", "low"), ("Apex Fuel", "5541", "Fuel", "US", "low"), ("BlueRoad Gas", "5541", "Fuel", "US", "low"),
    ("Luna Coffee", "5814", "Restaurants", "US", "low"), ("Basil & Co", "5812", "Restaurants", "US", "low"), ("Taco Corner", "5814", "Restaurants", "US", "low"),
    ("Metro Pizza", "5812", "Restaurants", "US", "low"), ("Sakura Ramen", "5812", "Restaurants", "US", "low"),
    ("Northwind Retail", "5311", "Department stores", "US", "low"), ("HomeBarn Hardware", "5200", "Home improvement", "US", "low"),
    ("PageTurn Books", "5942", "Books", "US", "low"), ("PetPals Supply", "5995", "Pet stores", "US", "low"), ("Urban Threads", "5651", "Clothing", "US", "low"),
    ("CityRide", "4121", "Ride share", "US", "low"), ("SkyJet Airlines", "4511", "Airlines", "US", "low"), ("Harbor Hotels", "7011", "Lodging", "US", "low"),
    ("PowerGrid Utilities", "4900", "Utilities", "US", "low"), ("ClearWave Mobile", "4814", "Telecom", "US", "low"), ("Evergreen Pharmacy", "5912", "Pharmacy", "US", "low"),
    ("StreamBox", "4899", "Streaming", "US", "low"), ("Tunely Music", "4899", "Streaming", "US", "low"), ("FitLife Gym", "7997", "Memberships", "US", "low"),
    ("Adobo Creative Cloud", "5734", "Software", "US", "low"), ("CloudNest Storage", "5734", "Software", "US", "low"),
    ("Parkline Garage", "7523", "Parking", "US", "low"), ("QuickWash Laundry", "7211", "Services", "US", "low"), ("Oakview Dental", "8021", "Healthcare", "US", "low"),
    ("TechZone Online", "5732", "Electronics", "US", "high"), ("CoinHarbor Exchange", "6051", "Crypto", "US", "high"), ("GiftCardMart", "5947", "Gift cards", "US", "high"),
    ("BetKing Online", "7995", "Gambling", "US", "high"), ("QuickSend Money Transfer", "4829", "Money transfer", "US", "high"), ("Skyline Duty Free", "5309", "Duty free", "RO", "medium"),
]
FOREIGN = ["RO", "NG", "VN", "BR", "ID"]
FRAUD_MERCHANTS = ["TechZone Online", "CoinHarbor Exchange", "GiftCardMart", "BetKing Online", "QuickSend Money Transfer"]
SUBSCRIPTIONS = ["StreamBox", "Tunely Music", "FitLife Gym", "Adobo Creative Cloud", "CloudNest Storage"]
EVERYDAY = [x[0] for x in MERCHANTS if x[4] == "low" and x[0] not in SUBSCRIPTIONS]


def _drop_everything(sync_conn) -> None:
    """Drop every table that exists (including ones from an older schema that no longer has a model)."""
    from sqlalchemy import MetaData
    md = MetaData()
    md.reflect(sync_conn)
    md.drop_all(sync_conn)


async def seed(n_customers: int = 300, seed_value: int = 42, today: date | None = None) -> dict:
    rnd = random.Random(seed_value)
    today = today or date.today()
    from app.db.models import Base
    from app.db.session import get_engine
    async with get_engine().begin() as conn:  # rebuild the schema so model changes always take effect
        await conn.run_sync(_drop_everything)
    await init_db()

    def at(days_ago: float, hour: int = 12, minute: int = 0) -> datetime:
        return datetime.combine(today - timedelta(days=int(days_ago)), datetime.min.time()) + timedelta(hours=hour, minutes=minute)

    customers: list[m.Customer] = []
    for i in range(1, n_customers + 1):
        fn, ln = rnd.choice(FIRST), rnd.choice(LAST)
        seg = rnd.choices(["standard", "premium", "private"], [0.70, 0.25, 0.05])[0]
        customers.append(m.Customer(id=f"CUST-{i:06d}", name=f"{fn} {ln}", email=f"{fn}.{ln}{i}@example.com".lower(), phone=f"+1 415 555 {rnd.randint(100, 199):04d}",
                                    segment=seg, kyc_status="verified", risk_flag="none", country="US", created_at=at(rnd.randint(150, 1500), 9)))
    by_id = {c.id: c for c in customers}

    # ---- scenario pools (disjoint, so ground truth is unambiguous) ----
    pool = [c for c in customers]
    rnd.shuffle(pool)
    spec = {"dup_posted": 12, "dup_hold": 8, "dup_large": 6, "dup_legit": 5, "dup_credited": 3, "dup_kyc_pending": 3, "dup_new_account": 3,
            "unrec_fraud": 8, "unrec_recurring": 5, "unrec_plain": 5, "lost_card": 6, "lost_card_fraud": 3,
            "transfer_pending": 5, "transfer_overdue": 4, "transfer_returned": 5, "wire_done": 2, "wire_pending": 2,
            "cancel_ok": 4, "cancel_wire": 3, "cancel_done": 2, "fee_waivable": 8, "fee_over_limit": 4, "fee_waiver_used": 5,
            "declined": 8, "aml_dup": 3, "aml_wire": 2, "repeat_contact": 6}
    sc: dict[str, list[m.Customer]] = {}
    for k, n in spec.items():
        sc[k], pool = pool[:n], pool[n:]
    manifest: dict[str, dict] = defaultdict(lambda: {"tags": [], "facts": {}})
    for tag, cs in sc.items():
        for c in cs:
            manifest[c.id]["tags"].append(tag)
    for c in sc["dup_kyc_pending"]:
        c.kyc_status = "pending"
    for c in sc["aml_dup"] + sc["aml_wire"]:
        c.risk_flag = "aml_review"
    merchants = [m.Merchant(id=f"MER-{i:06d}", name=n, mcc=mcc, category=cat, country=ctry, risk=risk) for i, (n, mcc, cat, ctry, risk) in enumerate(MERCHANTS, 1)]
    mer = {x.name: x for x in merchants}

    accounts: list[m.Account] = []
    cards: list[m.Card] = []
    txns: list[m.Transaction] = []
    transfers: list[m.Transfer] = []
    disputes: list[m.Dispute] = []
    alerts: list[m.FraudAlert] = []
    waivers: list[m.FeeWaiver] = []
    cnt = defaultdict(int)

    def nid(prefix: str, width: int) -> str:
        cnt[prefix] += 1
        return f"{prefix}-{cnt[prefix]:0{width}d}"

    new_ids = {x.id for x in sc["dup_new_account"]}
    chk: dict[str, m.Account] = {}
    deb: dict[str, m.Card] = {}
    cred_acct: dict[str, m.Account] = {}
    cred_card: dict[str, m.Card] = {}
    for c in customers:
        new_acct = c.id in new_ids
        opened = at(25, 10) if new_acct else c.created_at
        a = m.Account(id=nid("ACC", 8), customer_id=c.id, type="checking", number_last4=f"{rnd.randint(0, 9999):04d}", balance_cents=0, overdraft_limit_cents=50000 if c.segment != "standard" else 0,
                      status="active", opened_at=opened)
        accounts.append(a); chk[c.id] = a
        if rnd.random() < 0.6:
            accounts.append(m.Account(id=nid("ACC", 8), customer_id=c.id, type="savings", number_last4=f"{rnd.randint(0, 9999):04d}", balance_cents=rnd.randint(50_000, 4_000_000), status="active", opened_at=c.created_at))
        d = m.Card(id=nid("CARD", 7), customer_id=c.id, account_id=a.id, type="debit", network=rnd.choice(["visa", "mastercard"]), last4=f"{rnd.randint(0, 9999):04d}", status="active",
                   expiry=f"{rnd.randint(1, 12):02d}/{(today.year + rnd.randint(1, 4)) % 100:02d}", daily_limit_cents=250_000, contactless=1, intl_enabled=0, issued_at=opened)
        cards.append(d); deb[c.id] = d
        if rnd.random() < 0.3:
            ca = m.Account(id=nid("ACC", 8), customer_id=c.id, type="credit_card", number_last4=f"{rnd.randint(0, 9999):04d}", balance_cents=0, overdraft_limit_cents=500_000, status="active", opened_at=c.created_at)
            accounts.append(ca); cred_acct[c.id] = ca
            cc = m.Card(id=nid("CARD", 7), customer_id=c.id, account_id=ca.id, type="credit", network=rnd.choice(["visa", "mastercard"]), last4=f"{rnd.randint(0, 9999):04d}", status="active",
                        expiry=f"{rnd.randint(1, 12):02d}/{(today.year + rnd.randint(1, 4)) % 100:02d}", daily_limit_cents=500_000, contactless=1, intl_enabled=1, issued_at=c.created_at)
            cards.append(cc); cred_card[c.id] = cc
    # keep debit last4 distinct from credit last4 per customer
    for cid, cc in cred_card.items():
        while cc.last4 == deb[cid].last4:
            cc.last4 = f"{rnd.randint(0, 9999):04d}"

    def mk_txn(c, kind, direction, amount, desc, status, *, card=None, merchant=None, account=None, channel="pos", country="US", linked=None,
               created=None, decline=None) -> m.Transaction:
        created = created or at(rnd.randint(3, 90), rnd.randint(8, 21), rnd.randint(0, 59))
        acct = account or (next(a for a in accounts if a.id == card.account_id) if card else chk[c.id])
        t = m.Transaction(id=nid("TXN", 8), customer_id=c.id, account_id=acct.id,
                          card_id=card.id if card else None, kind=kind, direction=direction, amount_cents=amount, currency="USD", merchant_id=merchant.id if merchant else None,
                          description=desc, status=status, decline_reason=decline, channel=channel, country=country, linked_txn_id=linked, created_at=created,
                          posted_at=None if status in ("pending", "declined") else created + timedelta(hours=rnd.randint(2, 20)))
        txns.append(t)
        return t

    used: dict[str, set] = defaultdict(set)

    def purchase(c, merchant_name, amount, *, when=None, status="posted", card=None, channel="pos", country="US", decline=None, desc=None) -> m.Transaction:
        mm = mer[merchant_name]
        used[c.id].add(merchant_name)
        card = card or deb[c.id]
        return mk_txn(c, "card_purchase", "debit", amount, desc or mm.name, status, card=card, merchant=mm, channel="online" if mm.risk == "high" and channel == "pos" else channel,
                      country=country, created=when, decline=decline)

    # ---- background ledger ----
    for c in customers:
        lo, hi = (2, 22) if c.id in new_ids else (3, 88)  # a new account has no ledger from before it was opened
        pay = rnd.choice([180_000, 240_000, 320_000, 450_000, 620_000])
        for k in range(2 if c.id in new_ids else 7):  # biweekly payroll
            mk_txn(c, "payroll", "credit", pay, "Payroll deposit", "posted", account=chk[c.id], channel="app", created=at(k * 14 + rnd.randint(0, 3), 6))
        subs = [] if c.id in new_ids else rnd.sample(SUBSCRIPTIONS, rnd.randint(0, 2))
        for sname in subs:
            amt = rnd.choice([999, 1299, 1599, 2999, 5499])
            for k in range(3):
                purchase(c, sname, amt, when=at(k * 30 + 4, 3, rnd.randint(0, 40)), channel="online")
        for _ in range(rnd.randint(22, 38)):
            mn = rnd.choice(EVERYDAY)
            amt = rnd.randint(350, 14_500)
            card = cred_card.get(c.id) if c.id in cred_card and rnd.random() < 0.25 else None
            purchase(c, mn, amt, when=at(rnd.randint(lo, hi), rnd.randint(8, 21), rnd.randint(0, 59)), card=card)
        for _ in range(rnd.randint(0, 3)):
            mk_txn(c, "atm_withdrawal", "debit", rnd.choice([2000, 4000, 6000, 10000]), "ATM withdrawal", "posted", card=deb[c.id], channel="atm", created=at(rnd.randint(lo, min(hi, 80)), rnd.randint(9, 20)))
        for _ in range(rnd.randint(1, 3)):  # historic completed transfers
            amt = rnd.randint(5_000, 150_000)
            rail = rnd.choice(["ach", "ach", "internal"])
            when = at(rnd.randint(min(10, hi), min(hi, 80)), 11)
            tx = mk_txn(c, "transfer_out", "debit", amt, "Transfer to " + rnd.choice(FIRST) + " " + rnd.choice(LAST), "posted", account=chk[c.id], channel="app", created=when)
            transfers.append(m.Transfer(id=nid("TRF", 8), customer_id=c.id, from_account_id=chk[c.id].id, to_name=tx.description[12:], to_account_masked=f"••{rnd.randint(1000, 9999)}", rail=rail, amount_cents=amt,
                                        status="completed", reference=f"REF{rnd.randint(100000, 999999)}", txn_id=tx.id, initiated_at=when, completed_at=when + timedelta(days=1 if rail == "internal" else 2),
                                        expected_by=when + timedelta(days=3)))
        # pending authorisations from the last two days, for a living ledger
        for _ in range(rnd.randint(0, 2)):
            purchase(c, rnd.choice(EVERYDAY), rnd.randint(500, 9000), when=at(0, rnd.randint(7, 11), rnd.randint(0, 59)), status="pending")

    F = lambda c: manifest[c.id]["facts"]  # noqa: E731
    money = lambda cents: f"${cents / 100:,.2f}"  # noqa: E731

    # ---- T1 duplicate charges ----
    def dup_pair(c, merchant_name, amount, *, gap_hours=6, ago=None, second_status="posted", first_status="posted"):
        ago = ago if ago is not None else rnd.randint(3, 15)
        card = deb[c.id]
        a = purchase(c, merchant_name, amount, when=at(ago, 12, rnd.randint(0, 20)), status=first_status, card=card)
        b = purchase(c, merchant_name, amount, when=a.created_at + timedelta(hours=gap_hours), status=second_status, card=card)
        return a, b

    for c in sc["dup_posted"] + sc["aml_dup"]:
        amt = rnd.choice([1899, 2450, 3260, 4875, 7200, 12000, 18999, 24650, 38900])
        mn = rnd.choice(["Greenfield Grocers", "Apex Fuel", "Basil & Co", "Northwind Retail", "HomeBarn Hardware", "Urban Threads", "Evergreen Pharmacy"])
        a, b = dup_pair(c, mn, amt)
        F(c).update(original_txn_id=a.id, target_txn_id=b.id, merchant=mn, amount=money(amt), card_last4=deb[c.id].last4, expected="dispute_provisional_credit" if c.risk_flag == "none" else "human_review")
    for c in sc["dup_hold"]:
        amt = rnd.choice([2450, 4120, 5999, 8450, 15000])
        mn = rnd.choice(["Shell Station 4471", "BlueRoad Gas", "Luna Coffee", "Metro Pizza", "Taco Corner"])
        a, b = dup_pair(c, mn, amt, gap_hours=0, ago=1, first_status="posted", second_status="pending")
        b.created_at = a.created_at + timedelta(minutes=5)
        F(c).update(posted_txn_id=a.id, pending_txn_id=b.id, merchant=mn, amount=money(amt), expected="wait_pending_hold")
    for c in sc["dup_large"]:
        amt = rnd.choice([62000, 79900, 94500, 125000, 189900])
        a, b = dup_pair(c, rnd.choice(["SkyJet Airlines", "Harbor Hotels", "Northwind Retail", "HomeBarn Hardware"]), amt)
        mn = next(mm.name for mm in merchants if mm.id == b.merchant_id)
        F(c).update(original_txn_id=a.id, target_txn_id=b.id, merchant=mn, amount=money(amt), card_last4=deb[c.id].last4, expected="human_approval")
    for c in sc["dup_legit"]:
        amt = rnd.choice([1850, 2999, 4500, 6250])
        mn = rnd.choice(["Luna Coffee", "Greenfield Grocers", "CityRide", "Sakura Ramen"])
        a, b = dup_pair(c, mn, amt, gap_hours=72)
        F(c).update(txn_ids=[a.id, b.id], merchant=mn, amount=money(amt), gap_days=3, expected="not_duplicate")
    for c in sc["dup_credited"]:
        amt = rnd.choice([2200, 4999, 8800])
        mn = rnd.choice(["Basil & Co", "Greenfield Grocers", "Apex Fuel"])
        a, b = dup_pair(c, mn, amt, ago=rnd.randint(8, 20))
        pc = mk_txn(c, "provisional_credit", "credit", amt, "Provisional credit (dispute)", "posted", account=chk[c.id], channel="app", linked=b.id, created=b.created_at + timedelta(days=1))
        d = m.Dispute(id=nid("DSP", 6), customer_id=c.id, txn_id=b.id, reason="duplicate", amount_cents=amt, status="provisional_credit_issued", provisional_txn_id=pc.id, created_by="agent:payments",
                      created_at=pc.created_at)
        disputes.append(d)
        F(c).update(original_txn_id=a.id, target_txn_id=b.id, merchant=mn, amount=money(amt), dispute_id=d.id, expected="already_disputed")
    for c in sc["dup_kyc_pending"] + sc["dup_new_account"]:
        amt = rnd.choice([2450, 3600, 7500])
        mn = rnd.choice(["Greenfield Grocers", "Apex Fuel", "Basil & Co"])
        a, b = dup_pair(c, mn, amt, ago=rnd.randint(3, 8))
        F(c).update(original_txn_id=a.id, target_txn_id=b.id, merchant=mn, amount=money(amt), expected="human_approval",
                    why="kyc_pending" if c.kyc_status == "pending" else "account_too_new")

    # ---- T2 unrecognised payments ----
    for c in sc["unrec_fraud"]:
        country = rnd.choice(FOREIGN)
        ago = rnd.randint(1, 3)
        base = at(ago, 2, rnd.randint(0, 30))
        ids = []
        for j in range(rnd.choice([3, 4])):
            mn = FRAUD_MERCHANTS[(j + rnd.randint(0, 2)) % len(FRAUD_MERCHANTS)]
            t = purchase(c, mn, rnd.choice([21_200, 34_900, 48_000, 15_999]) + j * 111, when=base + timedelta(minutes=9 * j), channel="online", country=country)
            ids.append(t.id)
        tgt = txns[-1] if rnd.random() < 0.5 else next(t for t in txns if t.id == ids[0])
        alerts.append(m.FraudAlert(id=nid("FRD", 6), customer_id=c.id, txn_id=ids[0], signal="velocity", score=rnd.randint(80, 95), status="open", created_at=base + timedelta(minutes=30)))
        tm = next(mm.name for mm in merchants if mm.id == tgt.merchant_id)
        F(c).update(target_txn_id=tgt.id, txn_ids=ids, merchant=tm, amount=money(tgt.amount_cents), country=country, card_id=deb[c.id].id, card_last4=deb[c.id].last4, expected="block_card_dispute_replace")
    for c in sc["unrec_recurring"]:
        mn = rnd.choice(SUBSCRIPTIONS)
        amt = rnd.choice([1299, 1599, 2999, 5499])
        dates = []
        for k in range(1, 4):
            t = purchase(c, mn, amt, when=at(k * 30 + 6, 3, 10), channel="online"); dates.append(t.created_at.date().isoformat())
        t = purchase(c, mn, amt, when=at(5, 3, 10), channel="online")
        F(c).update(target_txn_id=t.id, merchant=mn, amount=money(amt), prior_dates=dates, expected="recognised_recurring")
    for c in sc["unrec_plain"]:
        mn = rnd.choice([n for n in EVERYDAY if n not in used[c.id]] or ["PageTurn Books"])  # a merchant this customer has never paid
        t = purchase(c, mn, rnd.choice([4650, 8900, 13_450, 21_100]), when=at(rnd.randint(4, 12), 15, 20))
        F(c).update(target_txn_id=t.id, merchant=mn, amount=money(t.amount_cents), expected="dispute_no_block")

    # ---- T3 lost cards ----
    for c in sc["lost_card"] + sc["lost_card_fraud"]:
        F(c).update(card_id=deb[c.id].id, card_last4=deb[c.id].last4, expected="block_card")
    for c in sc["lost_card_fraud"]:
        country = rnd.choice(FOREIGN)
        ids = [purchase(c, rnd.choice(FRAUD_MERCHANTS), 19_900 + j * 700, when=at(0, 5, 10 * j), channel="online", country=country, status="pending").id for j in range(3)]
        F(c).update(after_loss_txn_ids=ids, expected="block_card_and_dispute_offer")

    # ---- T4 transfers ----
    def mk_transfer(c, rail, amount, status, *, initiated, expected=None, return_code=None, completed=None, fee=0, to="Jordan Blake"):
        tx = mk_txn(c, "transfer_out", "debit", amount + fee, f"Transfer to {to}", "pending" if status in ("pending", "submitted") else "posted", account=chk[c.id], channel="app", created=initiated)
        t = m.Transfer(id=nid("TRF", 8), customer_id=c.id, from_account_id=chk[c.id].id, to_name=to, to_account_masked=f"••{rnd.randint(1000, 9999)}", rail=rail, amount_cents=amount, fee_cents=fee,
                       status=status, return_code=return_code, reference=f"REF{rnd.randint(100000, 999999)}", txn_id=tx.id, initiated_at=initiated, expected_by=expected, completed_at=completed)
        transfers.append(t)
        return t, tx

    for c in sc["transfer_pending"]:
        amt = rnd.choice([25_000, 48_000, 120_000, 310_000])
        init = at(1, 15)
        t, _ = mk_transfer(c, "ach", amt, "submitted", initiated=init, expected=datetime.combine(add_business_days(init.date(), 3), datetime.min.time()))
        F(c).update(transfer_id=t.id, rail="ach", amount=money(amt), to_name=t.to_name, expected_by=t.expected_by.date().isoformat(), expected="wait")
    for c in sc["transfer_overdue"]:
        amt = rnd.choice([55_000, 90_000, 240_000])
        init = at(8, 14)
        t, _ = mk_transfer(c, "ach", amt, "submitted", initiated=init, expected=datetime.combine(add_business_days(init.date(), 3), datetime.min.time()))
        F(c).update(transfer_id=t.id, rail="ach", amount=money(amt), to_name=t.to_name, expected="human_trace")
    for c in sc["transfer_returned"]:
        amt = rnd.choice([32_000, 77_500, 150_000])
        code = rnd.choice(["R01", "R02", "R03", "R04"])
        init = at(6, 10)
        t, tx = mk_transfer(c, "ach", amt, "returned", initiated=init, return_code=code, completed=init + timedelta(days=3))
        mk_txn(c, "transfer_in", "credit", amt, f"Returned transfer ({code})", "posted", account=chk[c.id], channel="app", linked=tx.id, created=init + timedelta(days=3, hours=2))
        F(c).update(transfer_id=t.id, rail="ach", amount=money(amt), to_name=t.to_name, return_code=code, expected="explain_return")
    for c in sc["wire_done"]:
        amt = rnd.choice([450_000, 900_000, 1_250_000])
        init = at(3, 10)
        t, _ = mk_transfer(c, "wire", amt, "completed", initiated=init, completed=init + timedelta(hours=3), fee=2500, to="Harbor Escrow LLC")
        F(c).update(transfer_id=t.id, rail="wire", amount=money(amt), to_name=t.to_name, expected="completed")
    for c in sc["wire_pending"] + sc["aml_wire"]:
        amt = 1_850_000 if c.risk_flag != "none" else rnd.choice([650_000, 980_000])
        init = at(5, 13) if c.risk_flag != "none" else at(0, 9)
        t, _ = mk_transfer(c, "wire", amt, "pending", initiated=init, fee=2500, to="Meridian Trading Ltd")
        F(c).update(transfer_id=t.id, rail="wire", amount=money(amt), to_name=t.to_name, expected="human_trace" if c.risk_flag != "none" else "wait", **({"internal_flag": "aml_review"} if c.risk_flag != "none" else {}))
    for c in sc["cancel_ok"]:
        amt = rnd.choice([40_000, 75_000, 210_000])
        t, _ = mk_transfer(c, "ach", amt, "pending", initiated=at(0, 8), expected=datetime.combine(add_business_days(today, 3), datetime.min.time()))
        F(c).update(transfer_id=t.id, rail="ach", amount=money(amt), to_name=t.to_name, expected="cancel_ok")
    for c in sc["cancel_wire"]:
        amt = rnd.choice([300_000, 520_000])
        t, _ = mk_transfer(c, "wire", amt, "pending", initiated=at(0, 9), fee=2500, to="Atlas Logistics")
        F(c).update(transfer_id=t.id, rail="wire", amount=money(amt), to_name=t.to_name, expected="cancel_denied_wire")
    for c in sc["cancel_done"]:
        amt = rnd.choice([40_000, 99_000])
        init = at(6, 11)
        t, _ = mk_transfer(c, "ach", amt, "completed", initiated=init, completed=init + timedelta(days=2), to="Casey Morgan")
        F(c).update(transfer_id=t.id, rail="ach", amount=money(amt), to_name=t.to_name, expected="cancel_denied_completed")

    # ---- T6 fees ----
    FEE_DESC = ["Overdraft fee", "Monthly maintenance fee", "Out-of-network ATM fee", "Returned item fee"]
    for c in sc["fee_waivable"]:
        d = rnd.choice(FEE_DESC)
        t = mk_txn(c, "fee", "debit", 3500, d, "posted", account=chk[c.id], channel="app", created=at(rnd.randint(3, 25), 2))
        F(c).update(fee_txn_id=t.id, amount="$35.00", description=d, expected="reverse_fee")
    for c in sc["fee_over_limit"]:
        t = mk_txn(c, "fee", "debit", 5000, "Extended overdraft fee", "posted", account=chk[c.id], channel="app", created=at(rnd.randint(3, 20), 2))
        F(c).update(fee_txn_id=t.id, amount="$50.00", description="Extended overdraft fee", expected="human_approval")
    for c in sc["fee_waiver_used"]:
        old = mk_txn(c, "fee", "debit", 3500, "Overdraft fee", "posted", account=chk[c.id], channel="app", created=at(100, 2))
        mk_txn(c, "refund", "credit", 3500, "Fee waiver", "posted", account=chk[c.id], channel="app", linked=old.id, created=at(98, 10))
        waivers.append(m.FeeWaiver(customer_id=c.id, fee_txn_id=old.id, waived_at=at(98, 10)))
        t = mk_txn(c, "fee", "debit", 3500, "Overdraft fee", "posted", account=chk[c.id], channel="app", created=at(rnd.randint(4, 15), 2))
        F(c).update(fee_txn_id=t.id, amount="$35.00", description="Overdraft fee", previous_waiver=old.id, expected="deny_waiver_used")

    # ---- T5 declines ----
    reasons = ["insufficient_funds", "daily_limit", "intl_disabled", "suspected_fraud", "card_blocked", "wrong_pin", "insufficient_funds", "daily_limit"]
    for c, why in zip(sc["declined"], reasons):
        mn = rnd.choice(["Northwind Retail", "SkyJet Airlines", "Urban Threads", "HomeBarn Hardware"])
        t = purchase(c, mn, rnd.choice([8_500, 15_900, 26_000]), when=at(rnd.randint(1, 4), 17, 30), status="declined", decline=why)
        F(c).update(target_txn_id=t.id, merchant=mn, amount=money(t.amount_cents), reason=why, expected="explain_decline")

    # ---- tickets (history + repeat contact) ----
    tn = 0
    tickets: list[m.Ticket] = []
    cats = [("payments", "Question about a transaction"), ("cards", "Card delivery question"), ("account", "Update address"), ("payments", "Statement request"), ("cards", "Change PIN")]
    for c in rnd.sample(customers, 80):
        for _ in range(rnd.randint(1, 2)):
            tn += 1
            cat, summ = rnd.choice(cats)
            tickets.append(m.Ticket(id=f"TCK-{tn:06d}", customer_id=c.id, summary=summ, category=cat, severity="low", status="resolved", created_at=at(rnd.randint(20, 200), 9)))
    for c in sc["repeat_contact"]:
        for j in range(3):
            tn += 1
            tickets.append(m.Ticket(id=f"TCK-{tn:06d}", customer_id=c.id, summary=rnd.choice(["Still not fixed", "Third time contacting the bank", "No reply to previous ticket"]), category=rnd.choice(["payments", "cards"]),
                                    severity="high", status="escalated" if j == 2 else "open", created_at=at(j * 3 + 1, 9)))
        F(c)["open_tickets"] = 3

    # ---- balances: opening balance + posted ledger ----
    posted_net: dict[str, int] = defaultdict(int)
    for t in txns:
        if t.status == "posted":
            posted_net[t.account_id] += t.amount_cents if t.direction == "credit" else -t.amount_cents
    for a in accounts:
        if a.type == "checking":
            a.balance_cents = rnd.randint(40_000, 900_000) + posted_net[a.id]
        elif a.type == "credit_card":
            a.balance_cents = posted_net[a.id]  # negative = amount owed

    components = [("Mobile app", "degraded", "Push notifications are delayed by a few minutes."), ("Online banking", "operational", ""), ("Card network", "operational", ""),
                  ("ACH & wire rails", "operational", ""), ("ATM network", "operational", ""), ("Fraud monitoring", "operational", "")]

    async with session_scope() as s:
        for tbl in (m.Verification, m.KgEdge, m.KgNode, m.Policy, m.FeeWaiver, m.FraudAlert, m.Dispute, m.Transfer, m.Transaction, m.Merchant, m.Card, m.Account, m.Ticket,
                    m.HumanReview, m.AuditLog, m.SecurityEvent, m.Conversation, m.ApiCredential, m.ServiceComponent, m.Customer, m.Meta, m.IdSequence):
            await s.execute(delete(tbl))
        s.add_all(customers); await s.flush()
        s.add_all(merchants); s.add_all(accounts); await s.flush()
        s.add_all(cards); await s.flush()
        s.add_all(txns); await s.flush()
        s.add_all(transfers); s.add_all(disputes); s.add_all(alerts); s.add_all(waivers); s.add_all(tickets)
        s.add_all([m.ServiceComponent(name=n, status=st, note=note) for n, st, note in components])
        s.add(m.Meta(key="business_today", value=today.isoformat()))
        s.add_all([m.IdSequence(name="DSP", value=len(disputes)), m.IdSequence(name="TCK", value=len(tickets)), m.IdSequence(name="HRQ", value=0), m.IdSequence(name="VER", value=0),
                   m.IdSequence(name="TXN", value=cnt["TXN"]), m.IdSequence(name="CARD", value=cnt["CARD"])])
        prows = [m.Policy(id=p["id"], title=p["title"], rule=p["rule"], params=json.dumps(p["params"]), regulation=p["regulation"], kb_article=p["kb"], version="1.0") for p in DEFAULT_POLICIES]
        s.add_all(prows); await s.flush()
        slugs = sorted(p.stem for p in (ROOT / "kb").glob("*.md"))
        nodes, edges = kg.knowledge_layer(prows, slugs)
        s.add_all([m.KgNode(id=n["id"], type=n["type"], label=n["label"], props=json.dumps(n["props"])) for n in nodes]); await s.flush()
        s.add_all([m.KgEdge(src=a, dst=b, rel=r) for a, b, r in edges])

        creds: dict[str, str] = {}
        for who, role, cid in [("staff-alice", "agent_staff", None), ("admin", "admin", None)]:
            sec = secrets.token_urlsafe(12); creds[who] = sec
            s.add(m.ApiCredential(client_id=who, secret_hash=hash_secret(sec), role=role, customer_id=cid))
        for c in customers:
            sec = secrets.token_urlsafe(12); creds[c.id] = sec
            s.add(m.ApiCredential(client_id=c.id, secret_hash=hash_secret(sec, iterations=30_000), role="customer", customer_id=c.id))

    from sqlalchemy import text
    async with session_scope() as s:  # fold the WAL into the main file so plain file copies are safe
        await s.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
    out = {"today": today.isoformat(), "customers": len(customers), "accounts": len(accounts), "cards": len(cards), "transactions": len(txns), "transfers": len(transfers),
           "disputes": len(disputes), "fraud_alerts": len(alerts), "policies": len(prows), "kg_nodes": len(nodes), "kg_edges": len(edges), "tickets": len(tickets),
           "scenarios": {k: len(v) for k, v in sc.items()}}
    data = ROOT / "data"
    (data / "seed_manifest.json").write_text(json.dumps({"summary": out, "customers": manifest}, indent=1))
    (data / "demo_credentials.json").write_text(json.dumps(creds, indent=1))
    return out


if __name__ == "__main__":
    async def _main():
        res = await seed()
        print(json.dumps(res, indent=1))
    asyncio.run(_main())
