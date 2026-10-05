"""Slack integration (Events API + interactivity). Works without a bot token: outgoing messages then go to
logs/slack_outbox.jsonl (dry-run) so the flow is fully testable/inspectable offline.

Guardrails:  G-SLACK-01 request signature (HMAC-SHA256, 5-min replay window, constant-time compare)
             G-SLACK-02 retry de-duplication (Slack re-sends events if we answer slowly; we ack immediately and process async)
             G-SLACK-03 only Slack users linked to an Orbit Bank account may use the bot; only linked STAFF may press review buttons
             G-SLACK-04 customer text shown in the escalation channel is PII-masked
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs

import httpx
from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from sqlalchemy import select

from app.core.config import get_settings
from app.db import models as m
from app.db.session import session_scope
from app.graph import helpers as H
from app.graph import runner
from app.guardrails.events import log_security_event
from app.guardrails.pii import mask_pii

router = APIRouter(prefix="/slack", tags=["slack"])
_seen_events: dict[str, float] = {}
_bg: set[asyncio.Task] = set()
UNLINKED_REPLY = "Your Slack account isn't linked to an Orbit Bank account yet, so I can't help here. Please contact support@orbit.example to link it."


def verify_signature(body: bytes, timestamp: str | None, signature: str | None, secret: str, now: float | None = None) -> bool:
    if not (timestamp and signature and secret):
        return False
    try:
        if abs((now or time.time()) - int(timestamp)) > 300:
            return False
    except ValueError:
        return False
    base = f"v0:{timestamp}:".encode() + body
    expected = "v0=" + hmac.new(secret.encode(), base, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def _require_signature(request: Request) -> bytes:
    body = await request.body()
    secret = get_settings().slack_signing_secret
    if not verify_signature(body, request.headers.get("X-Slack-Request-Timestamp"), request.headers.get("X-Slack-Signature"), secret):
        await log_security_event("slack_bad_signature", f"path={request.url.path}", blocked=True)
        raise HTTPException(401, "invalid slack signature")
    return body


# ------------------------------------------------------------------ outbound
async def slack_api(method: str, payload: dict) -> dict:
    s = get_settings()
    if not s.slack_bot_token:
        d = s.log_dir
        d.mkdir(parents=True, exist_ok=True)
        with (d / "slack_outbox.jsonl").open("a") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "method": method, "payload": payload}, default=str) + "\n")
        return {"ok": True, "dry_run": True}
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"https://slack.com/api/{method}", json=payload, headers={"Authorization": f"Bearer {s.slack_bot_token}"})
        return r.json()


def review_blocks(row: m.HumanReview) -> list[dict]:
    t = lambda x: {"type": "mrkdwn", "text": x}
    return [
        {"type": "header", "text": {"type": "plain_text", "text": f"Human review {row.id} ({row.priority})"}},
        {"type": "section", "fields": [t(f"*Customer*\n{row.customer_id}"), t(f"*Reason*\n{(row.reason or '')[:150]}")]},
        {"type": "section", "text": t("*Customer message*\n> " + mask_pii(row.customer_message or "")[:600].replace("\n", "\n> "))},
        {"type": "section", "text": t("*Summary*\n" + mask_pii(row.summary or "", kinds={"secret", "card", "ssn", "iban"})[:700])},
        {"type": "section", "text": t("*Draft reply (not sent)*\n" + mask_pii(row.draft_reply or "_none, please write one_", kinds={"secret", "card", "ssn", "iban"})[:700])},
        {"type": "actions", "block_id": f"review:{row.id}", "elements": [
            {"type": "button", "action_id": "review_approve", "style": "primary", "text": {"type": "plain_text", "text": "Approve draft"}, "value": row.id},
            {"type": "button", "action_id": "review_edit", "text": {"type": "plain_text", "text": "Edit & send"}, "value": row.id},
            {"type": "button", "action_id": "review_reject", "style": "danger", "text": {"type": "plain_text", "text": "Reject"}, "value": row.id}]},
    ]


async def notify_review(review_id: str):
    row = await H.get_review_by_id(review_id)
    if row and row.status == "pending":
        await slack_api("chat.postMessage", {"channel": get_settings().slack_review_channel, "text": f"Human review {row.id}", "blocks": review_blocks(row)})


async def notify_resolution(query_id: str, reply: str, status: str):
    """Deliver a human-resolved answer back to the originating Slack thread (API-channel customers poll GET /v1/query/{id})."""
    async with session_scope() as s:
        conv = await s.get(m.Conversation, query_id)
    if conv and conv.channel == "slack" and conv.channel_ref:
        channel, _, thread = conv.channel_ref.partition(":")
        await slack_api("chat.postMessage", {"channel": channel, "thread_ts": thread or None, "text": reply})


# ------------------------------------------------------------------ inbound
async def _user_for_slack(slack_user: str) -> m.ApiCredential | None:
    async with session_scope() as s:
        return (await s.execute(select(m.ApiCredential).where(m.ApiCredential.slack_user_id == slack_user))).scalars().first()


async def handle_message(event: dict):
    slack_user, channel = event.get("user"), event.get("channel")
    thread = event.get("thread_ts") or event.get("ts")
    text = re.sub(r"<@[A-Z0-9]+>", "", event.get("text") or "").strip()
    cred = await _user_for_slack(slack_user) if slack_user else None
    if not cred or cred.role != "customer" or not cred.customer_id:
        await log_security_event("slack_unlinked_user", f"slack_user={slack_user}", blocked=True)
        await slack_api("chat.postMessage", {"channel": channel, "thread_ts": thread, "text": UNLINKED_REPLY})
        return
    res = await runner.run_query(query_id=uuid.uuid4().hex, customer_id=cred.customer_id, message=text, channel="slack",
                                 channel_ref=f"{channel}:{thread}")
    await slack_api("chat.postMessage", {"channel": channel, "thread_ts": thread, "text": res["reply"] or "(no reply)"})


@router.post("/events")
async def slack_events(request: Request, bg: BackgroundTasks):
    body = await _require_signature(request)
    payload = json.loads(body or b"{}")
    if payload.get("type") == "url_verification":
        return {"challenge": payload.get("challenge")}
    if request.headers.get("X-Slack-Retry-Num"):  # G-SLACK-02: we already acked the original delivery
        return {"ok": True, "retry": "ignored"}
    eid = payload.get("event_id")
    now = time.time()
    for k in [k for k, t in _seen_events.items() if now - t > 600]:
        del _seen_events[k]
    if eid and eid in _seen_events:
        return {"ok": True, "duplicate": True}
    if eid:
        _seen_events[eid] = now
    ev = payload.get("event") or {}
    is_dm = ev.get("type") == "message" and ev.get("channel_type") == "im"
    if (ev.get("type") == "app_mention" or is_dm) and not ev.get("bot_id") and not ev.get("subtype"):
        t = asyncio.create_task(handle_message(ev))  # ack within Slack's 3 s limit; process async
        _bg.add(t)
        t.add_done_callback(_bg.discard)
    return {"ok": True}


async def _staff_for(slack_user: str) -> m.ApiCredential | None:
    cred = await _user_for_slack(slack_user)
    return cred if cred and cred.role in ("agent_staff", "admin") else None


@router.post("/interactions")
async def slack_interactions(request: Request):
    body = await _require_signature(request)
    payload = json.loads(parse_qs(body.decode()).get("payload", ["{}"])[0])
    slack_user = (payload.get("user") or {}).get("id", "")
    staff = await _staff_for(slack_user)
    if not staff:  # G-SLACK-03
        await log_security_event("slack_unauthorized_review_action", f"slack_user={slack_user}", blocked=True)
        return {"response_type": "ephemeral", "text": "You are not authorised to resolve reviews."}
    if payload.get("type") == "block_actions":
        act = payload["actions"][0]
        rid = act.get("value")
        if act["action_id"] == "review_edit":
            row = await H.get_review_by_id(rid)
            await slack_api("views.open", {"trigger_id": payload.get("trigger_id"), "view": {
                "type": "modal", "callback_id": "review_edit_submit", "private_metadata": rid,
                "title": {"type": "plain_text", "text": "Edit reply"}, "submit": {"type": "plain_text", "text": "Send"},
                "blocks": [{"type": "input", "block_id": "reply", "label": {"type": "plain_text", "text": "Reply to customer"},
                            "element": {"type": "plain_text_input", "action_id": "text", "multiline": True, "initial_value": (row.draft_reply or "")[:2000] if row else ""}}]}})
            return {"ok": True}
        action = "approve" if act["action_id"] == "review_approve" else "reject"
        return await _resolve(rid, action, staff.client_id, None)
    if payload.get("type") == "view_submission" and payload["view"]["callback_id"] == "review_edit_submit":
        reply = payload["view"]["state"]["values"]["reply"]["text"]["value"]
        return await _resolve(payload["view"]["private_metadata"], "edit", staff.client_id, reply)
    return {"ok": True}


async def _resolve(review_id: str, action: str, reviewer: str, reply: str | None):
    try:
        res = await runner.resume_review(review_id=review_id, action=action, reviewer=reviewer, reply=reply)
    except (KeyError, ValueError) as e:
        return {"response_type": "ephemeral", "text": f"Cannot resolve: {e}"}
    await notify_resolution(res["query_id"], res["reply"], res["status"])
    return {"response_type": "in_channel", "replace_original": True, "text": f"Review {review_id} {action}d by {reviewer}"}
