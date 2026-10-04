"""Demo helper for the public UI: lists a few synthetic accounts with their generated credentials. Only when DEMO_MODE=true."""
import json

from fastapi import APIRouter, HTTPException

from app.core.config import ROOT, get_settings

router = APIRouter(tags=["demo"])
PICKS = [("double_charge", "Double-charged customer", "Has two identical invoices; try asking for a refund"), ("failed_payment", "Failed payment", "Latest invoice failed; ask why"),
         ("refund_small_ok", "Refund-eligible", "Recent payment under $100: auto-approved refunds"), ("refund_needs_approval", "Refund needs approval", "Payment over $100: goes to a human"),
         ("webhook_timeouts", "Webhook problems", "Endpoint timing out; platform incident active"), ("api_errors_429", "Rate-limited API user", "Hits plan limits"),
         ("annual_outside_window", "Annual plan, outside refund window", "Refund is declined by policy"), ("repeat_contact", "Frustrated repeat contact", "3 open tickets")]


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
            out.append({"label": label, "hint": hint, "client_id": cid, "secret": creds[cid], "role": "customer"})
    out.append({"label": "Support staff", "hint": "Review queue: approve, edit or reject AI drafts", "client_id": "staff-alice", "secret": creds["staff-alice"], "role": "agent_staff"})
    return out
