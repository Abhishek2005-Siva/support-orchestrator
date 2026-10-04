"""Deterministic synthetic backend. `python -m app.db.seed` (re)builds data/support.db.

~200 customers, ~1000 invoices, plus deliberate edge cases. Every edge case is recorded in
data/seed_manifest.json with the facts an eval needs (invoice ids, amounts, ...), so the golden
dataset has ground truth that does not depend on any LLM.

SYNTHETIC DATA ONLY. Names/emails/phones/cards are fabricated (example.com, 555-01xx).
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
from app.tools.rules import PLAN_PRICES_CENTS

FIRST = "Aiden Bella Carlos Dana Elias Fatima Gabriel Hana Ivan Julia Kenji Lena Mateo Nora Omar Priya Quinn Rosa Samir Tara Umar Vera Wen Xavi Yara Zane Amara Bruno Chloe Dmitri Esme Farid Grace Hugo Ines Jamal Keira Liam Mina".split()
LAST = "Alvarez Berg Chen Dubois Evans Fischer Garcia Haddad Ito Jensen Khan Lopez Moreau Nowak Okafor Petrov Quist Rossi Silva Tanaka Usman Varga Weber Xu Yilmaz Zhang Abbott Brandt Costa Dahl Egan Ford Gill Holm Iyer Joshi Klein Lund".split()
PLANS = ["free", "starter", "pro", "business", "enterprise"]
PLAN_W = [0.10, 0.35, 0.35, 0.15, 0.05]
DEC_REASONS = ["card_declined", "insufficient_funds", "expired_card"]
COUNTRIES = ["US", "US", "US", "GB", "DE", "FR", "IN", "CA", "AU", "BR"]


def _plan_desc(plan: str, cycle: str, period: str) -> str:
    return f"{plan.capitalize()} plan – {'Annual' if cycle == 'annual' else 'Monthly'} subscription ({period})"


def _period(d: date) -> str:
    return f"{d.year}-{d.month:02d}"


def _add_months(d: date, n: int) -> date:
    y, mo = divmod(d.month - 1 + n, 12)
    return date(d.year + y, mo + 1, min(d.day, 28))


async def seed(n_customers: int = 200, seed_value: int = 42, today: date | None = None) -> dict:
    rnd = random.Random(seed_value)
    today = today or date.today()
    from app.db.models import Base
    from app.db.session import get_engine
    async with get_engine().begin() as conn:  # rebuild the schema so model changes always take effect
        await conn.run_sync(Base.metadata.drop_all)
    await init_db()
    async with session_scope() as s:
        for tbl in (m.ErrorLog, m.Ticket, m.RefundRequest, m.Payment, m.Invoice, m.HumanReview, m.AuditLog,
                    m.SecurityEvent, m.Conversation, m.ApiCredential, m.ServiceComponent, m.Customer, m.Meta, m.IdSequence):
            await s.execute(delete(tbl))

        customers: list[m.Customer] = []
        for i in range(1, n_customers + 1):
            plan = rnd.choices(PLANS, PLAN_W)[0]
            cycle = "monthly"
            fn, ln = rnd.choice(FIRST), rnd.choice(LAST)
            tier = "vip" if plan == "enterprise" else ("premium" if plan == "business" else "standard")
            exp_year = today.year + rnd.randint(1, 4)
            customers.append(m.Customer(
                id=f"CUST-{i:06d}", name=f"{fn} {ln}", email=f"{fn}.{ln}{i}@example.com".lower(),
                phone=f"+1 415 555 {rnd.randint(100, 199):04d}", tier=tier, plan=plan, billing_cycle=cycle,
                account_status="active", card_last4=None if plan == "free" else f"{rnd.randint(0, 9999):04d}",
                card_expiry=None if plan == "free" else f"{rnd.randint(1, 12):02d}/{exp_year % 100:02d}",
                country=rnd.choice(COUNTRIES), created_at=datetime.combine(today - timedelta(days=rnd.randint(120, 900)), datetime.min.time())))
        by_id = {c.id: c for c in customers}

        # ---- assign scenarios (disjoint pools so ground truth is unambiguous) ----
        paid = [c for c in customers if c.plan in ("starter", "pro")]
        biz = [c for c in customers if c.plan == "business"]
        ent = [c for c in customers if c.plan == "enterprise"]
        rnd.shuffle(paid), rnd.shuffle(biz), rnd.shuffle(ent)
        manifest: dict[str, dict] = defaultdict(lambda: {"tags": [], "facts": {}})

        def take(pool, n):
            out, pool[:] = pool[:n], pool[n:]
            return out

        sc = {
            "double_charge": take(paid, 14), "failed_payment": take(paid, 14), "expired_card": take(paid, 10),
            "annual_outside_window": take(paid, 10), "already_refunded": take(paid, 6), "suspended": take(paid, 6),
            "refund_small_ok": take(paid, 16), "repeat_contact": take(paid, 10),
            "api_errors_429": take(paid, 8), "auth_errors_401": take(paid, 6), "webhook_timeouts": take(paid, 6),
            "sso_errors": take(biz, 5), "refund_needs_approval": take(biz, 6), "vip_refund": take(ent, 5),
        }
        for tag, cs in sc.items():
            for c in cs:
                manifest[c.id]["tags"].append(tag)
        for c in sc["annual_outside_window"]:
            c.billing_cycle = "annual"
        for c in sc["suspended"]:
            c.account_status = "suspended"
        for c in sc["expired_card"]:
            c.card_expiry = f"{rnd.randint(1, 8):02d}/{(today.year - 1) % 100:02d}"

        invoices: list[m.Invoice] = []
        payments: list[m.Payment] = []
        inv_n = pay_n = 0

        def mk_invoice(c, issued: datetime, amount: int, status: str, period: str, cycle=None) -> m.Invoice:
            nonlocal inv_n
            inv_n += 1
            inv = m.Invoice(id=f"INV-{inv_n:08d}", customer_id=c.id, amount_cents=amount, currency="USD",
                            description=_plan_desc(c.plan, cycle or c.billing_cycle, period), period=period,
                            issued_at=issued, status=status)
            invoices.append(inv)
            return inv

        def mk_payment(inv, status, at: datetime, reason=None, amount=None) -> m.Payment:
            nonlocal pay_n
            pay_n += 1
            p = m.Payment(id=f"PAY-{pay_n:08d}", invoice_id=inv.id, customer_id=inv.customer_id,
                          amount_cents=amount if amount is not None else inv.amount_cents, status=status,
                          failure_reason=reason, created_at=at)
            payments.append(p)
            return p

        scen_of = {c.id: set(manifest[c.id]["tags"]) for c in customers}
        for c in customers:
            if c.plan == "free":
                continue
            price = PLAN_PRICES_CENTS[c.plan]
            tags = scen_of[c.id]
            annual = c.billing_cycle == "annual"
            if annual:  # one annual invoice, paid 20-60 days ago -> outside the 14-day annual window
                age = rnd.randint(20, 60)
                issued = datetime.combine(today - timedelta(days=age), datetime.min.time()) + timedelta(hours=9)
                inv = mk_invoice(c, issued, price * 10, "paid", _period(issued.date()))
                mk_payment(inv, "succeeded", issued + timedelta(minutes=2))
                manifest[c.id]["facts"].update(invoice_id=inv.id, amount=f"${inv.amount_cents / 100:,.2f}", days_ago=age, window=14)
                continue
            anchor = rnd.randint(1, 28)
            # newest invoice date: most recent occurrence of billing day (>= 1 day ago)
            last = date(today.year, today.month, min(anchor, 28))
            if last >= today:
                last = _add_months(last, -1)
            if "refund_small_ok" in tags or "refund_needs_approval" in tags or "vip_refund" in tags or "double_charge" in tags:
                last = today - timedelta(days=rnd.randint(2, 20))  # firmly inside the 30-day window
            for k in range(5):
                d = _add_months(last, -k) if k else last
                if k and ("refund_small_ok" in tags or "refund_needs_approval" in tags or "vip_refund" in tags or "double_charge" in tags):
                    d = last - timedelta(days=30 * k)
                issued = datetime.combine(d, datetime.min.time()) + timedelta(hours=rnd.randint(1, 20), minutes=rnd.randint(0, 59))
                period = _period(d)
                status, reason = "paid", None
                if k == 0 and ("failed_payment" in tags or "expired_card" in tags or "suspended" in tags):
                    status = "failed"
                    reason = "expired_card" if "expired_card" in tags else rnd.choice(DEC_REASONS)
                if k == 0 and "already_refunded" in tags:
                    status = "refunded"
                inv = mk_invoice(c, issued, price, status, period)
                if status == "failed":
                    mk_payment(inv, "failed", issued + timedelta(minutes=1), reason)
                    manifest[c.id]["facts"].update(invoice_id=inv.id, amount=f"${price / 100:,.2f}", failure_reason=reason)
                elif status == "refunded":
                    mk_payment(inv, "refunded", issued + timedelta(minutes=1))
                    manifest[c.id]["facts"].update(invoice_id=inv.id, amount=f"${price / 100:,.2f}")
                else:
                    p = mk_payment(inv, "succeeded", issued + timedelta(minutes=1))
                    if k == 0:
                        manifest[c.id]["facts"].setdefault("invoice_id", inv.id)
                        manifest[c.id]["facts"].setdefault("amount", f"${price / 100:,.2f}")
                        manifest[c.id]["facts"].setdefault("days_ago", (today - p.created_at.date()).days)
                if k == 0 and "double_charge" in tags:
                    dup = mk_invoice(c, issued + timedelta(hours=6), price, "paid", period)
                    mk_payment(dup, "succeeded", issued + timedelta(hours=6, minutes=1))
                    manifest[c.id]["facts"].update(original_invoice_id=inv.id, duplicate_invoice_id=dup.id,
                                                   invoice_id=dup.id, amount=f"${price / 100:,.2f}")

        # ---- tickets (history + repeat-contact scenario) ----
        tn = 0
        tickets: list[m.Ticket] = []
        cats = [("billing", "Question about invoice"), ("technical", "API returns errors"), ("account", "Update account email"),
                ("technical", "Dashboard slow"), ("billing", "Change payment method")]
        for c in rnd.sample(customers, 60):
            for _ in range(rnd.randint(1, 2)):
                tn += 1
                cat, summ = rnd.choice(cats)
                tickets.append(m.Ticket(id=f"TCK-{tn:06d}", customer_id=c.id, summary=summ, category=cat, severity="low",
                                        status="resolved", created_at=datetime.combine(today - timedelta(days=rnd.randint(20, 200)), datetime.min.time())))
        for c in sc["repeat_contact"]:
            for j in range(3):
                tn += 1
                tickets.append(m.Ticket(id=f"TCK-{tn:06d}", customer_id=c.id, summary=rnd.choice(["Still not fixed", "Third time contacting support", "No reply to previous ticket"]),
                                        category=rnd.choice(["billing", "technical"]), severity="high", status="escalated" if j == 2 else "open",
                                        created_at=datetime.combine(today - timedelta(days=j * 3 + 1), datetime.min.time())))
            manifest[c.id]["facts"]["open_tickets"] = 3

        # ---- error logs per technical scenario ----
        logs: list[m.ErrorLog] = []
        spec = {
            "api_errors_429": ("RATE_LIMIT_429", "Rate limit exceeded: 1000 requests/min", "WARN"),
            "auth_errors_401": ("AUTH_401_INVALID_TOKEN", "Invalid or expired API key presented", "ERROR"),
            "webhook_timeouts": ("WEBHOOK_TIMEOUT", "Webhook endpoint did not respond within 10s", "ERROR"),
            "sso_errors": ("SSO_SAML_ASSERTION_EXPIRED", "SAML assertion rejected: NotOnOrAfter in the past (clock skew)", "ERROR"),
        }
        for tag, (code, msg, lvl) in spec.items():
            for c in sc[tag]:
                for j in range(rnd.randint(4, 9)):
                    logs.append(m.ErrorLog(customer_id=c.id, ts=datetime.combine(today, datetime.min.time()) - timedelta(hours=rnd.randint(1, 60)),
                                           level=lvl, code=code, message=msg))
                manifest[c.id]["facts"]["error_code"] = code
        for c in rnd.sample([c for c in customers if not scen_of[c.id] and c.plan != "free"], 25):  # background noise
            logs.append(m.ErrorLog(customer_id=c.id, ts=datetime.combine(today, datetime.min.time()) - timedelta(hours=rnd.randint(30, 300)),
                                   level="INFO", code="REQUEST_OK_SLOW", message="Request completed in 2.4s"))

        components = [("API", "operational", ""), ("Dashboard", "operational", ""), ("Billing", "operational", ""),
                      ("Authentication", "operational", ""), ("Data Export", "operational", ""),
                      ("Webhooks", "degraded", "Delayed webhook delivery (up to 15 min). Engineers are investigating.")]

        s.add_all(customers); await s.flush()
        s.add_all(invoices); await s.flush()
        s.add_all(payments); s.add_all(tickets); s.add_all(logs)
        s.add_all([m.ServiceComponent(name=n, status=st, note=note) for n, st, note in components])
        s.add(m.Meta(key="business_today", value=today.isoformat()))
        s.add_all([m.IdSequence(name="REF", value=0), m.IdSequence(name="TCK", value=len(tickets)), m.IdSequence(name="HRQ", value=0)])

        # ---- credentials (demo only; secrets are written to a git-ignored file) ----
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
    out = {"today": today.isoformat(), "customers": len(customers), "invoices": len(invoices), "payments": len(payments),
           "tickets": len(tickets), "error_logs": len(logs),
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
