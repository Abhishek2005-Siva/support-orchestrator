"""Demo helper for the public UI: lists a few synthetic accounts with their generated credentials. Only when DEMO_MODE=true."""
import json

from fastapi import APIRouter, HTTPException

from app.core.config import ROOT, get_settings

router = APIRouter(tags=["demo"])
PICKS = [("dup_posted", "Charged twice at a merchant", "Two identical card purchases both posted: a duplicate. Ask to get the money back"),
         ("dup_hold", "Pending hold + charge", "Looks like two charges but one is only a hold: no dispute needed"),
         ("dup_large", "Large double charge", "Duplicate over $500: provisional credit needs a human"),
         ("unrec_fraud", "Suspicious foreign payments", "Card used abroad in minutes: block, dispute, replace"),
         ("unrec_recurring", "Payment they don't recognise", "Actually a monthly subscription"),
         ("lost_card", "Lost card", "Block the card immediately"),
         ("transfer_pending", "ACH transfer in transit", "Still inside the normal 1-3 business days"),
         ("transfer_returned", "Returned transfer", "The receiving bank rejected it"),
         ("cancel_ok", "Cancellable transfer", "Pending and not yet submitted"),
         ("cancel_wire", "Wire (cannot be cancelled)", "Wires are irrevocable"),
         ("fee_waivable", "Fee to waive", "Within the courtesy-waiver policy"),
         ("fee_waiver_used", "Fee waiver already used", "Policy refuses a second waiver in 12 months"),
         ("declined", "Declined payment", "Explain the decline reason"),
         ("aml_dup", "Customer with an internal flag", "The agent must never reveal internal reviews"),
         ("repeat_contact", "Frustrated repeat contact", "3 open tickets")]


@router.get("/demo/accounts")
async def demo_accounts():
    if not get_settings().demo_mode:
        raise HTTPException(404, "demo mode disabled")
    d = ROOT / "data"
    man = json.loads((d / "seed_manifest.json").read_text())["customers"]
    creds = json.loads((d / "demo_credentials.json").read_text())
    out = []
    for tag, label, hint in PICKS:
        cid = next((c for c, v in man.items() if tag in v["tags"]), None)
        if cid:
            out.append({"key": tag, "label": label, "hint": hint, "client_id": cid, "secret": creds[cid], "role": "customer"})
    out.append({"key": "staff", "label": "Support staff", "hint": "Review queue: approve, edit or reject AI drafts", "client_id": "staff-alice", "secret": creds["staff-alice"], "role": "agent_staff"})
    return out
