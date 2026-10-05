"""Knowledge graph in two layers.

KNOWLEDGE layer (materialised in kg_nodes / kg_edges, built at seed time from the policies table and the help articles):
    issue -REQUIRES_CHECK-> check        issue -GOVERNED_BY-> policy       policy -BASED_ON-> regulation
    issue -RESOLVED_BY-> action          policy -DOCUMENTED_IN-> kb        action -GATED_BY-> policy
The verifier reads this layer to decide WHICH checks to run and WHICH policies to cite, so editing the graph changes behaviour.

OPERATIONAL layer (derived live from the bank tables, so it can never be stale; always scoped to one customer):
    customer -OWNS-> account / card     account -HAS_TXN-> transaction -AT-> merchant      transaction -DISPUTED_BY-> dispute
    transfer -FROM-> account            transaction -USES-> card
"""
from __future__ import annotations

import json

from sqlalchemy import select

from app.db import models as m
from app.db.session import session_scope

ISSUES: dict[str, dict] = {
    "duplicate_charge": {"label": "Duplicate / double charge",
                         "checks": ["same_card_merchant_amount", "pending_vs_posted", "already_disputed_or_credited", "dispute_window", "provisional_credit_eligibility", "aml_flag"],
                         "policies": ["POL-DUP-01", "POL-HOLD-01", "POL-DSP-01", "POL-DSP-02", "POL-DSP-03", "POL-AML-01"], "actions": ["file_dispute"]},
    "unrecognised_payment": {"label": "Payment the customer does not recognise",
                             "checks": ["merchant_history", "geo_mismatch", "velocity", "high_risk_merchant", "open_fraud_alert", "card_status", "already_disputed_or_credited",
                                        "dispute_window", "provisional_credit_eligibility", "aml_flag"],
                             "policies": ["POL-FRD-01", "POL-DSP-01", "POL-DSP-02", "POL-DSP-03", "POL-LIA-01", "POL-AML-01"], "actions": ["file_dispute", "block_card", "request_replacement_card"]},
    "lost_stolen_card": {"label": "Lost or stolen card", "checks": ["card_status"], "policies": ["POL-LIA-01"], "actions": ["block_card", "request_replacement_card"]},
    "transfer_trace": {"label": "Transfer not arrived / pending / returned", "checks": ["rail_timing", "return_code", "aml_flag"],
                       "policies": ["POL-ACH-01", "POL-WIR-01", "POL-AML-01"], "actions": ["assign_to_human"]},
    "transfer_cancel": {"label": "Cancel a transfer", "checks": ["cancellable", "rail_timing"], "policies": ["POL-CAN-01", "POL-WIR-01"], "actions": ["cancel_transfer"]},
    "fee_dispute": {"label": "Fee the customer wants reversed", "checks": ["fee_type_limit", "waiver_history", "account_standing", "already_disputed_or_credited", "aml_flag"],
                    "policies": ["POL-FEE-01", "POL-AML-01"], "actions": ["reverse_fee"]},
    "declined_payment": {"label": "Declined payment", "checks": ["decline_reason", "card_status"], "policies": ["POL-LIM-01", "POL-FRD-01"], "actions": []},
}
ACTION_GATES = {"file_dispute": ["POL-DSP-01", "POL-DSP-02", "POL-DSP-03"], "block_card": ["POL-LIA-01", "POL-FRD-01"], "request_replacement_card": ["POL-LIA-01"],
                "reverse_fee": ["POL-FEE-01"], "cancel_transfer": ["POL-CAN-01"], "assign_to_human": ["POL-AML-01"]}
ISSUE_TYPES = tuple(ISSUES)
INTERNAL_POLICIES = {"POL-AML-01", "POL-KYC-01"}
INTERNAL_CHECKS = {"aml_flag"}


def knowledge_layer(policy_rows, kb_slugs: list[str]) -> tuple[list[dict], list[tuple[str, str, str]]]:
    """(nodes, edges) for seeding."""
    nodes: dict[str, dict] = {}
    edges: list[tuple[str, str, str]] = []

    def node(i, typ, label, **props):
        nodes.setdefault(i, {"id": i, "type": typ, "label": label, "props": props})

    pol = {p.id: p for p in policy_rows}
    for pid, p in pol.items():
        node(f"pol:{pid}", "policy", f"{pid} {p.title}", rule=p.rule, params=json.loads(p.params), version=p.version)
        node(f"reg:{p.regulation}", "regulation", p.regulation)
        edges.append((f"pol:{pid}", f"reg:{p.regulation}", "BASED_ON"))
        if p.kb_article and p.kb_article in kb_slugs:
            edges.append((f"pol:{pid}", f"kb:{p.kb_article}", "DOCUMENTED_IN"))
    for s in kb_slugs:
        node(f"kb:{s}", "kb", s.replace("-", " "))
    for key, d in ISSUES.items():
        node(f"issue:{key}", "issue", d["label"])
        for c in d["checks"]:
            node(f"chk:{c}", "check", c.replace("_", " "))
            edges.append((f"issue:{key}", f"chk:{c}", "REQUIRES_CHECK"))
        for p in d["policies"]:
            if p in pol:
                edges.append((f"issue:{key}", f"pol:{p}", "GOVERNED_BY"))
        for a in d["actions"]:
            node(f"act:{a}", "action", a)
            edges.append((f"issue:{key}", f"act:{a}", "RESOLVED_BY"))
    for a, ps in ACTION_GATES.items():
        node(f"act:{a}", "action", a)
        for p in ps:
            if p in pol:
                edges.append((f"act:{a}", f"pol:{p}", "GATED_BY"))
    return list(nodes.values()), edges


async def plan(issue_type: str) -> dict:
    """What the graph says about an issue type: checks to run (in order), policies, regulations, actions, KB articles, and the paths read."""
    async with session_scope() as s:
        out = (await s.execute(select(m.KgEdge).where(m.KgEdge.src == f"issue:{issue_type}").order_by(m.KgEdge.id))).scalars().all()
        pols = [e.dst[4:] for e in out if e.rel == "GOVERNED_BY"]
        sec = (await s.execute(select(m.KgEdge).where(m.KgEdge.src.in_([f"pol:{p}" for p in pols])).order_by(m.KgEdge.id))).scalars().all() if pols else []
    checks = [e.dst[4:] for e in out if e.rel == "REQUIRES_CHECK"]
    actions = [e.dst[4:] for e in out if e.rel == "RESOLVED_BY"]
    regs = sorted({e.dst[4:] for e in sec if e.rel == "BASED_ON"})
    kb = sorted({e.dst[3:] for e in sec if e.rel == "DOCUMENTED_IN"})
    paths = [f"issue:{issue_type} -{e.rel}-> {e.dst}" for e in out] + [f"{e.src} -{e.rel}-> {e.dst}" for e in sec]
    # the customer-safe view: compliance rules (AML / KYC) shape decisions but are never shown to the model or the customer
    hide_pol = {p for p in pols if p in INTERNAL_POLICIES}
    pub_sec = [e for e in sec if e.src[4:] not in hide_pol]
    pub = {"checks": [c for c in checks if c not in INTERNAL_CHECKS], "policies": [p for p in pols if p not in hide_pol],
           "regulations": sorted({e.dst[4:] for e in pub_sec if e.rel == "BASED_ON"}), "kb": sorted({e.dst[3:] for e in pub_sec if e.rel == "DOCUMENTED_IN"}),
           "paths": [f"issue:{issue_type} -{e.rel}-> {e.dst}" for e in out if e.dst[4:] not in INTERNAL_CHECKS and e.dst[4:] not in hide_pol] + [f"{e.src} -{e.rel}-> {e.dst}" for e in pub_sec]}
    return {"issue": issue_type, "checks": checks, "policies": pols, "regulations": regs, "actions": actions, "kb": kb, "paths": paths, "public": pub}


async def entity_view(customer_id: str, entity_id: str) -> dict:
    """Neighbourhood of one entity for the query_knowledge_graph tool: an issue type (knowledge layer) or one of the customer's own ids."""
    if entity_id.startswith("issue:") or entity_id in ISSUES:
        return await plan(entity_id.removeprefix("issue:"))
    g = await graph_view(customer_id, focus=entity_id, depth=1, max_txn=60)
    near = [e for e in g["edges"] if entity_id in (e["src"], e["dst"])]
    ids = {e["src"] for e in near} | {e["dst"] for e in near}
    return {"entity": entity_id, "related": [{"id": n["id"], "type": n["type"], "label": n["label"]} for n in g["nodes"] if n["id"] in ids and n["id"] != entity_id],
            "relations": [f"{e['src']} -{e['rel']}-> {e['dst']}" for e in near]}


async def graph_view(customer_id: str | None, focus: str | None = None, depth: int = 2, max_txn: int = 30, include_knowledge: bool = True) -> dict:
    """Nodes and edges for the UI / tool. The operational layer is customer-scoped; the knowledge layer is global."""
    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def n(i, typ, label, layer, **props):
        nodes.setdefault(i, {"id": i, "type": typ, "label": label, "layer": layer, "props": props})

    def e(a, b, rel):
        edges.append({"src": a, "dst": b, "rel": rel})

    async with session_scope() as s:
        if include_knowledge:
            for r in (await s.execute(select(m.KgNode))).scalars().all():
                n(r.id, r.type, r.label, "knowledge")
            for r in (await s.execute(select(m.KgEdge).order_by(m.KgEdge.id))).scalars().all():
                e(r.src, r.dst, r.rel)
        if customer_id:
            c = await s.get(m.Customer, customer_id)
            if c:
                n(f"cust:{c.id}", "customer", f"{c.name} ({c.id})", "operational", segment=c.segment, kyc=c.kyc_status)
                accts = (await s.execute(select(m.Account).where(m.Account.customer_id == customer_id))).scalars().all()
                for a in accts:
                    n(f"acct:{a.id}", "account", f"{a.type} ••{a.number_last4}", "operational", status=a.status)
                    e(f"cust:{c.id}", f"acct:{a.id}", "OWNS")
                for cd in (await s.execute(select(m.Card).where(m.Card.customer_id == customer_id))).scalars().all():
                    n(f"card:{cd.id}", "card", f"{cd.network} ••{cd.last4} ({cd.status})", "operational", status=cd.status)
                    e(f"cust:{c.id}", f"card:{cd.id}", "OWNS")
                    e(f"card:{cd.id}", f"acct:{cd.account_id}", "DRAWS_ON")
                txns = (await s.execute(select(m.Transaction).where(m.Transaction.customer_id == customer_id).order_by(m.Transaction.created_at.desc()).limit(max_txn))).scalars().all()
                mers = {x.id: x for x in (await s.execute(select(m.Merchant))).scalars().all()}
                for t in txns:
                    n(f"txn:{t.id}", "transaction", f"{t.description[:28]} {t.amount_cents / 100:,.2f}", "operational", status=t.status, kind=t.kind)
                    e(f"acct:{t.account_id}", f"txn:{t.id}", "HAS_TXN")
                    if t.card_id:
                        e(f"txn:{t.id}", f"card:{t.card_id}", "USES")
                    if t.merchant_id and t.merchant_id in mers:
                        mm = mers[t.merchant_id]
                        n(f"mer:{mm.id}", "merchant", mm.name, "operational", risk=mm.risk)
                        e(f"txn:{t.id}", f"mer:{mm.id}", "AT")
                for d in (await s.execute(select(m.Dispute).where(m.Dispute.customer_id == customer_id))).scalars().all():
                    n(f"dsp:{d.id}", "dispute", f"{d.id} {d.status}", "operational", reason=d.reason)
                    e(f"txn:{d.txn_id}", f"dsp:{d.id}", "DISPUTED_BY")
                for t in (await s.execute(select(m.Transfer).where(m.Transfer.customer_id == customer_id).order_by(m.Transfer.initiated_at.desc()).limit(10))).scalars().all():
                    n(f"trf:{t.id}", "transfer", f"{t.rail} {t.amount_cents / 100:,.2f} {t.status}", "operational", status=t.status)
                    e(f"trf:{t.id}", f"acct:{t.from_account_id}", "FROM")
    ok = {x["id"] for x in nodes.values()}
    edges = [x for x in edges if x["src"] in ok and x["dst"] in ok]
    if focus:  # keep only the neighbourhood of `focus` up to `depth` hops
        keep, frontier = {focus, f"txn:{focus}", f"card:{focus}", f"trf:{focus}", f"issue:{focus}"} & ok, None
        frontier = set(keep)
        for _ in range(depth):
            nxt = {x["dst"] for x in edges if x["src"] in frontier} | {x["src"] for x in edges if x["dst"] in frontier}
            frontier = nxt - keep
            keep |= nxt
        nodes = {k: v for k, v in nodes.items() if k in keep}
        edges = [x for x in edges if x["src"] in keep and x["dst"] in keep]
    return {"nodes": list(nodes.values()), "edges": edges}
