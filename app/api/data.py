"""Read-only views of the bank's data layer for the console's "Data & knowledge" page: tables, policies, verifications, knowledge graph.
Customers see only their own rows (global reference tables are readable); staff can look at any customer. Internal columns (risk_flag, fraud
scores, credentials, audit and security logs) are never exposed here. Guardrail id: G-API-06 (data views are scoped and column-allow-listed)."""
from __future__ import annotations

import json
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select

from app.core.deps import User, current_user
from app.db import models as m
from app.db.session import session_scope
from app.tools import kg

router = APIRouter(prefix="/v1/data", tags=["data"])

# table -> (group, description, scope column or None for global reference data, hidden columns)
TABLES: dict[str, tuple[str, str, str | None, set[str]]] = {
    "customers": ("bank", "Customers (KYC status, segment)", "id", {"risk_flag"}),
    "accounts": ("bank", "Checking, savings and credit-card accounts", "customer_id", set()),
    "cards": ("bank", "Debit, credit and virtual cards with controls", "customer_id", set()),
    "merchants": ("reference", "Merchant directory (category, country, risk)", None, set()),
    "transactions": ("bank", "The ledger: every purchase, ATM withdrawal, transfer leg, fee, refund and credit", "customer_id", set()),
    "transfers": ("bank", "ACH / wire / internal transfers and their status", "customer_id", set()),
    "disputes": ("bank", "Disputes with provisional credit", "customer_id", set()),
    "fraud_alerts": ("bank", "Fraud-engine alerts (score hidden)", "customer_id", {"score"}),
    "fee_waivers": ("bank", "Courtesy fee waivers already used", "customer_id", set()),
    "tickets": ("bank", "Support tickets", "customer_id", set()),
    "service_components": ("reference", "Live status of channels", None, set()),
    "policies": ("knowledge", "Bank policies as data, linked to regulations and help articles", None, set()),
    "kg_nodes": ("knowledge", "Knowledge graph: issue types, checks, policies, regulations, actions, articles", None, set()),
    "kg_edges": ("knowledge", "Knowledge graph relations", None, set()),
    "verifications": ("knowledge", "Every verification the agents ran: what was consulted and decided", "customer_id", set()),
}
MODELS = {t.__tablename__: t for t in (m.Customer, m.Account, m.Card, m.Merchant, m.Transaction, m.Transfer, m.Dispute, m.FraudAlert, m.FeeWaiver, m.Ticket, m.ServiceComponent, m.Policy, m.KgNode, m.KgEdge, m.Verification)}


def _val(v):
    if isinstance(v, datetime):
        return v.isoformat(timespec="minutes")
    return v


def _cid(user: User, customer_id: str | None) -> str | None:
    if user.role == "customer":
        return user.customer_id
    return customer_id  # staff may look at any customer (or all, when None)


@router.get("/overview")
async def overview(user: User = Depends(current_user), customer_id: str | None = Query(None, pattern=r"^CUST-\d{6}$")):
    cid = _cid(user, customer_id)
    out = []
    async with session_scope() as s:
        for name, (group, desc, scope, hidden) in TABLES.items():
            mdl = MODELS[name]
            q = select(func.count()).select_from(mdl)
            if scope and cid:
                q = q.where(getattr(mdl, scope) == cid)
            n = (await s.execute(q)).scalar_one()
            total = (await s.execute(select(func.count()).select_from(mdl))).scalar_one() if scope and cid else n
            cols = [{"name": c.name, "type": str(c.type), "fk": next((f.target_fullname for f in c.foreign_keys), None), "pk": c.primary_key}
                    for c in mdl.__table__.columns if c.name not in hidden]
            out.append({"name": name, "group": group, "description": desc, "rows": n, "bank_total": total, "columns": cols, "scoped": bool(scope and cid)})
    return {"customer_id": cid, "tables": out}


@router.get("/table/{name}")
async def table(name: str, limit: int = Query(25, ge=1, le=100), q: str | None = Query(None, max_length=60), user: User = Depends(current_user),
                customer_id: str | None = Query(None, pattern=r"^CUST-\d{6}$")):
    if name not in TABLES:
        raise HTTPException(404, "unknown table")
    _, _, scope, hidden = TABLES[name]
    mdl = MODELS[name]
    cid = _cid(user, customer_id)
    query = select(mdl)
    if scope and cid:
        query = query.where(getattr(mdl, scope) == cid)
    for order in ("created_at", "initiated_at", "id"):
        if hasattr(mdl, order):
            query = query.order_by(getattr(mdl, order).desc())
            break
    async with session_scope() as s:
        rows = (await s.execute(query.limit(500 if q else limit))).scalars().all()
    cols = [c.name for c in mdl.__table__.columns if c.name not in hidden]
    data = [{c: _val(getattr(r, c)) for c in cols} for r in rows]
    if q:
        ql = q.lower()
        data = [r for r in data if ql in json.dumps(r, default=str).lower()][:limit]
    for r in data:
        if name == "policies" and "params" in r:
            r["params"] = json.loads(r["params"])
        if name == "verifications" and "report" in r:
            r["report"] = json.loads(r["report"])
            r["report"].pop("internal", None)
    return {"table": name, "columns": cols, "rows": data, "count": len(data)}


@router.get("/policies")
async def policies(user: User = Depends(current_user)):
    async with session_scope() as s:
        rows = (await s.execute(select(m.Policy).order_by(m.Policy.id))).scalars().all()
        edges = (await s.execute(select(m.KgEdge).where(m.KgEdge.rel == "GOVERNED_BY"))).scalars().all()
        gates = (await s.execute(select(m.KgEdge).where(m.KgEdge.rel == "GATED_BY"))).scalars().all()
    issues: dict[str, list[str]] = {}
    for e in edges:
        issues.setdefault(e.dst[4:], []).append(e.src[6:])
    acts: dict[str, list[str]] = {}
    for e in gates:
        acts.setdefault(e.dst[4:], []).append(e.src[4:])
    hide = {"POL-AML-01", "POL-KYC-01"} if user.role == "customer" else set()
    return {"policies": [{"id": r.id, "title": r.title, "rule": r.rule, "params": json.loads(r.params), "regulation": r.regulation, "kb_article": r.kb_article, "version": r.version,
                          "governs": issues.get(r.id, []), "gates_actions": acts.get(r.id, [])} for r in rows if r.id not in hide]}


@router.get("/verifications")
async def verifications(limit: int = Query(10, ge=1, le=50), user: User = Depends(current_user), customer_id: str | None = Query(None, pattern=r"^CUST-\d{6}$")):
    cid = _cid(user, customer_id)
    async with session_scope() as s:
        q = select(m.Verification).order_by(m.Verification.created_at.desc()).limit(limit)
        if cid:
            q = q.where(m.Verification.customer_id == cid)
        rows = (await s.execute(q)).scalars().all()
    out = []
    for r in rows:
        rep = json.loads(r.report)
        if user.role == "customer":
            rep.pop("internal", None)
        out.append({"verification_id": r.id, "issue_type": r.issue_type, "subject_id": r.subject_id, "decision": r.decision, "action": r.action, "created_at": _val(r.created_at), "report": rep})
    return {"verifications": out}


@router.get("/graph")
async def graph(focus: str | None = Query(None, max_length=40, pattern=r"^[A-Za-z0-9:_\-. ]+$"), depth: int = Query(2, ge=1, le=3), txns: int = Query(12, ge=0, le=40),
                knowledge: bool = True, user: User = Depends(current_user), customer_id: str | None = Query(None, pattern=r"^CUST-\d{6}$")):
    cid = _cid(user, customer_id)
    g = await kg.graph_view(cid, focus=focus, depth=depth, max_txn=txns, include_knowledge=knowledge)
    if user.role == "customer":  # compliance policies/checks are internal
        hide = {f"pol:{p}" for p in kg.INTERNAL_POLICIES} | {f"chk:{c}" for c in kg.INTERNAL_CHECKS} | {"reg:BSA / AML"}
        g["nodes"] = [n for n in g["nodes"] if n["id"] not in hide]
        g["edges"] = [e for e in g["edges"] if e["src"] not in hide and e["dst"] not in hide]
    return g
