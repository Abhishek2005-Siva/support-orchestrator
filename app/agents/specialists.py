"""Payments / Cards / General specialists = ReAct engine + agent-specific prompt, tools and context.

Staged deterministic prefetch (all reads except the last, gated steps) so the model does not have to discover the workflow:
  stage 1  ledger / cards / transfers + a knowledge-base search            (read)
  stage 2  VERIFY the issue the customer describes                           (read + verification record)
  stage 3  IF the verification allows it AND the customer asked for it: the action(s)   (file_dispute / reverse_fee / cancel_transfer / block_card)
  stage 4  replacement card, only if the customer asked
Every step goes through the normal guarded tool path (G-TOOL-01..13), so the gates still apply. The model then explains the outcome
instead of sometimes promising an action it never performs."""
from __future__ import annotations

import re

from app.agents import prompts
from app.agents.react import run_react_agent
from app.agents.rules import ACTION_REQUEST_RX, BLOCK_RX, CANCEL_RX, DISPUTE_RX, FEE_RX, REPLACE_RX, UNAUTH_RX
from app.schemas.models import SpecialistResponse
from app.tools.handlers import h_profile, NoArgs
from app.tools.runtime import ToolContext, business_today


async def fetch_profile(customer_id: str) -> dict:
    """Deterministic prefetch (saves one LLM round-trip per specialist): segment / verification status."""
    ctx = ToolContext(customer_id=customer_id, query_id="prefetch", agent="general")
    r = await h_profile(ctx, NoArgs())
    return r.data if r.ok else {}


_TRANSFERISH = re.compile(r"transfer|wire|\bach\b|bank transfer|sent (?:money|a payment|funds)|payment to|remittance|haven'?t (?:received|got)|not (?:arrived|received)|hasn'?t arrived|standing order", re.I)


_ACCOUNT_SPECIFIC = re.compile(r"\bmy\b|\bI\b|\bI'm\b|\bI've\b|\bme\b|\$\s?\d|\b(?:last|latest|yesterday|today|recent)\b|\bwe\b|\bour\b|TXN-|TRF-|CARD-", re.I)


def prefetch_plan(agent: str, message: str, sub_question: str | None = None) -> list[tuple[str, dict]]:
    q = (sub_question or message).strip()[:300]
    if len(q) < 3:
        q = "help"
    kb = ("search_knowledge_base", {"query": q})
    if agent in ("payments", "cards") and not _ACCOUNT_SPECIFIC.search(message):
        return [kb]      # a how-to / policy question needs no account data (and stays cacheable)
    if agent == "payments":
        plan = [("get_transactions", {"days": 60, "limit": 16}), kb]
        if _TRANSFERISH.search(message):
            plan.insert(1, ("get_transfer_status", {}))
        return plan
    if agent == "cards":
        return [("get_cards", {}), ("get_transactions", {"days": 45, "limit": 16}), kb]
    return [kb]


# ---------------------------------------------------------------- picking the subject from the customer's words
_STOP = set("charged twice payment payments card money account bank today yesterday please refund there which think made again double duplicate transaction transactions purchase bought from with have this that what were been "
            "your would could about check unauthorised unauthorized recognise recognize don't dont didn't didnt just also last week month someone fraud lost stolen waive fee want need back cancel transfer declined reversed "
            "charge charges taken took money's amount cards debit credit online shop store".split())
_AMT = re.compile(r"\$\s?(\d[\d,]*(?:\.\d{1,2})?)|\b(\d[\d,]*\.\d{2})\b")


def _cents(s: str) -> int | None:
    try:
        return int(round(float(s.replace("$", "").replace(",", "").strip()) * 100))
    except ValueError:
        return None


def _amounts(message: str) -> set[int]:
    return {c for g in _AMT.findall(message) for c in [_cents(g[0] or g[1])] if c is not None}


def _pick(message: str, rows: list[dict], key: str, prefer=lambda r: True):
    """Row whose amount or description the customer mentioned; else (only if unambiguous by `prefer`) the newest preferred row."""
    cand = [r for r in rows if prefer(r)]
    amts = _amounts(message)
    hit = [r for r in cand if _cents(r.get("amount", "")) in amts] if amts else []
    if not hit:
        words = {w for w in re.findall(r"[a-z0-9]{4,}", message.lower()) if w not in _STOP}
        scored = [(len(words & set(re.findall(r"[a-z0-9]{4,}", r.get("description", "").lower()))), r) for r in cand]
        best = max((s for s, _ in scored), default=0)
        hit = [r for s, r in scored if s and s == best]
    return (hit[0] if hit else None), bool(hit)


def _rows_of(results, name):
    for n, r in results:
        if n == name and r.ok and r.data:
            return r.data.get("transactions") or r.data.get("transfers") or r.data.get("cards") or []
    return []


_DUP = re.compile(r"twice|two (?:times|charges|payments|identical)|double|duplicate|again|same (?:amount|charge|payment)|2 (?:times|charges)|charged (?:me )?(?:2|two)|extra charge", re.I)
_FEE = re.compile(r"\bfees?\b|overdraft|maintenance charge|atm charge|returned item", re.I)
_LOST = re.compile(r"\blost\b|stolen|stole|missing|misplaced|swallowed|robbed|mugged|pickpocket|left (?:it|my card)|can'?t find my card|compromis", re.I)
_DECL = re.compile(r"declin|refused|rejected|didn'?t go through|did not go through|wouldn'?t work|not (?:accepted|going through)|failed", re.I)


def _dup_group(rows):
    seen: dict[tuple, list[dict]] = {}
    for r in rows:
        if r.get("kind") == "card_purchase" and r.get("direction") == "debit" and r.get("status") in ("posted", "pending"):
            seen.setdefault((r["description"], r["amount"], r.get("card_last4")), []).append(r)
    return [g for g in seen.values() if len(g) > 1]


def staged_followup(agent: str, message: str, action_requested: bool):
    """Returns fn(results) -> next stage (list of (tool, args)) or None. `results` holds the PREVIOUS stage only, so state is kept in the closure."""
    memo: dict = {}
    ask = bool(DISPUTE_RX.search(message) or UNAUTH_RX.search(message)) and action_requested is not False

    def verify(issue, sid):
        return [("verify_transaction_issue", {"issue_type": issue, "subject_id": sid})]

    def stage2(results):
        txns = _rows_of(results, "get_transactions")
        trfs = _rows_of(results, "get_transfer_status")
        cards = _rows_of(results, "get_cards")
        memo.update(txns=txns, trfs=trfs, cards=cards)
        if agent == "payments":
            if _TRANSFERISH.search(message) and trfs:
                amts = _amounts(message)
                pick = next((t for t in trfs if _cents(t.get("amount", "")) in amts), None) if amts else None
                if CANCEL_RX.search(message):
                    pick = pick or next((t for t in trfs if t["status"] in ("pending", "submitted")), trfs[0])
                    return verify("transfer_cancel", pick["transfer_id"])
                if not pick:
                    live = [t for t in trfs if t["status"] in ("pending", "submitted", "returned", "failed")]
                    pick = (live or trfs)[0]
                return verify("transfer_trace", pick["transfer_id"])
            if _FEE.search(message):
                fees = [r for r in txns if r.get("kind") == "fee"]
                if fees:
                    pick, _ = _pick(message, fees, "txn_id")
                    return verify("fee_dispute", (pick or fees[0])["txn_id"])
                memo["need_fee_lookup"] = True
                return [("get_transactions", {"kind": "fee", "days": 90, "limit": 6})]
            if _DUP.search(message) and not UNAUTH_RX.search(message):
                groups = _dup_group(txns)
                if groups:
                    amts = _amounts(message)
                    words = {w for w in re.findall(r"[a-z0-9]{4,}", message.lower()) if w not in _STOP}
                    best = None
                    for g in groups:
                        s = (1 if _cents(g[0]["amount"]) in amts else 0) + len(words & set(re.findall(r"[a-z0-9]{4,}", g[0]["description"].lower())))
                        if best is None or s > best[0]:
                            best = (s, g)
                    return verify("duplicate_charge", best[1][0]["txn_id"])
                pick, hit = _pick(message, txns, "txn_id", prefer=lambda r: r.get("kind") == "card_purchase" and r.get("status") != "declined")
                if hit:
                    return verify("duplicate_charge", pick["txn_id"])
            return None
        if agent == "cards":
            active = [c for c in cards if c["status"] == "active"]
            if UNAUTH_RX.search(message) and (_amounts(message) or re.search(r"[a-z]{4,}", message)):
                pick, hit = _pick(message, txns, "txn_id", prefer=lambda r: r.get("kind") in ("card_purchase", "atm_withdrawal") and r.get("direction") == "debit")
                if hit:
                    return verify("unrecognised_payment", pick["txn_id"])
            if _LOST.search(message) or BLOCK_RX.search(message) and not UNAUTH_RX.search(message):
                want = "credit" if re.search(r"credit", message, re.I) else "debit" if re.search(r"debit", message, re.I) else None
                cand = [c for c in active if (c["type"] == want)] if want else active
                if len(cand) == 1 or (want is None and len(active) == 1):
                    c = (cand or active)[0]
                    memo["card"] = c
                    return verify("lost_stolen_card", c["card_id"])
                return None
            if _DECL.search(message):
                dec = [r for r in txns if r.get("status") == "declined"]
                if dec:
                    pick, _ = _pick(message, dec, "txn_id")
                    return verify("declined_payment", (pick or dec[0])["txn_id"])
        return None

    def after_verify(results):
        res = next((r for n, r in results if n == "verify_transaction_issue" and r.ok and r.data), None)
        if not res:
            return None
        d = res.data
        if d["decision"] != "act":
            return None
        vid, acts = d["verification_id"], d.get("allowed_actions", [])
        memo["vid"], memo["acts"] = vid, acts
        calls = []
        if d["issue_type"] == "lost_stolen_card":
            c = memo.get("card")
            if c:
                reason = "stolen" if re.search(r"stol|robbed|mugged|pickpocket", message, re.I) else "lost"
                calls.append(("block_card", {"card_id": c["card_id"], "reason": reason}))
            return calls or None
        if "block_card" in acts and d.get("block_card") and BLOCK_RX.search(message):
            subj = next((t for t in memo.get("txns", []) if t["txn_id"] == d["subject_id"]), None)
            card = next((c for c in memo.get("cards", []) if c["last4"] == (subj or {}).get("card_last4") and c["status"] == "active"), None)
            if card:
                memo["card"] = card
                calls.append(("block_card", {"card_id": card["card_id"], "reason": "fraud"}))
        for tool, gate in (("file_dispute", ask), ("reverse_fee", bool(FEE_RX.search(message)) and action_requested is not False), ("cancel_transfer", bool(CANCEL_RX.search(message)))):
            if tool in acts and gate:
                calls.append((tool, {"verification_id": vid} if tool != "file_dispute" else {"verification_id": vid, "note": message.strip()[:180]}))
        return calls or None

    def after_actions(results):
        done = {n for n, r in results if r.ok}
        card = memo.get("card")
        if "block_card" in done and card and REPLACE_RX.search(message):
            return [("request_replacement_card", {"card_id": card["card_id"]})]
        return None

    def fn(results):
        names = {n for n, _ in results}
        if "get_transactions" in names or "get_cards" in names or "get_transfer_status" in names:
            if names <= {"get_transactions"} and memo.get("need_fee_lookup"):  # the stage-2 fee lookup just returned
                memo["need_fee_lookup"] = False
                fees = _rows_of(results, "get_transactions")
                if fees:
                    pick, _ = _pick(message, fees, "txn_id")
                    return verify("fee_dispute", (pick or fees[0])["txn_id"])
                return None
            return stage2(results)
        if "verify_transaction_issue" in names:
            return after_verify(results)
        if names & {"file_dispute", "block_card", "reverse_fee", "cancel_transfer"}:
            return after_actions(results)
        return None
    return fn


async def run_specialist(agent: str, *, message: str, customer_id: str, query_id: str, profile: dict | None = None,
                         dispatch: dict | None = None, history: list[dict] | None = None, feedback: list[str] | None = None,
                         foreign_ids: list[str] | None = None, ctx: ToolContext | None = None, sub_question: str | None = None,
                         prior_actions: list[dict] | None = None) -> SpecialistResponse:
    profile = profile if profile is not None else await fetch_profile(customer_id)
    action = (dispatch or {}).get("action_requested") if dispatch else None
    ctx = ctx or ToolContext(customer_id=customer_id, query_id=query_id, agent=agent, message=message, action_requested=action)
    for e in prior_actions or []:  # actions an earlier attempt already performed: keep them as evidence (the validator grounds claims in it)
        ctx.evidence.append(e)
        if e.get("tool") == "file_dispute" and (e.get("result") or {}).get("status") == "pending_approval":
            ctx.flags["requires_human_approval"] = True
    if profile:  # the verified profile is shown to the model in its prompt, so it must be visible to the validator as evidence
        ctx.evidence.append({"tool": "get_customer_profile", "args": {}, "source": "db:customer", "result": profile})
    today = (await business_today()).isoformat()
    ctx.evidence.append({"tool": "context", "args": {}, "source": "ctx:today", "result": {"today": today}})
    user = prompts.specialist_user(message, profile, today, dispatch, history, feedback, foreign_ids, agent, sub_question)
    return await run_react_agent(agent, prompts.specialist_system(agent), user, ctx, prefetch=prefetch_plan(agent, message, sub_question),
                                 followup=staged_followup(agent, message, action) if agent in ("payments", "cards") else None)
