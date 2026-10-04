import asyncio
import json
import uuid

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from sse_starlette.sse import EventSourceResponse

from app.core import metrics
from app.core.config import get_settings
from app.core.deps import User, current_user, customer_user
from app.db import models as m
from app.db.session import session_scope
from app.graph import runner
from app.schemas.models import QueryRequest, QueryResponse

router = APIRouter(prefix="/v1", tags=["query"])
_bg: set[asyncio.Task] = set()
_sem: asyncio.Semaphore | None = None


def _gate() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(get_settings().max_inflight_queries)
    return _sem


async def _run(user: User, body: QueryRequest, query_id: str) -> dict:
    sem = _gate()
    try:  # bounded concurrency: shed load instead of exhausting memory / the LLM rate limit
        await asyncio.wait_for(sem.acquire(), timeout=5)
    except asyncio.TimeoutError:
        raise HTTPException(503, "server busy, retry shortly", headers={"Retry-After": "5"})
    metrics.INFLIGHT.inc()
    try:
        res = await runner.run_query(query_id=query_id, customer_id=user.customer_id, message=body.message, channel=body.channel,
                                     history=body.history)
    finally:
        metrics.INFLIGHT.dec()
        sem.release()
    metrics.QUERIES.labels(res["status"], body.channel).inc()
    metrics.LATENCY.observe(res["latency_ms"] / 1000)
    for r in res["flags"].get("input_reasons", []):
        metrics.GUARD.labels(r).inc()
    return res


def _resp(res: dict) -> QueryResponse:
    return QueryResponse(query_id=res["query_id"], status=res["status"], reply=res["reply"], intents=res["intents"],
                         review_id=res["review_id"], latency_ms=res["latency_ms"], flags=res["flags"])


@router.post("/query", response_model=QueryResponse)
async def submit_query(body: QueryRequest, user: User = Depends(customer_user)):
    """Submit a support query as the authenticated customer. `customer_id` ALWAYS comes from the JWT, never from the body."""
    query_id = uuid.uuid4().hex
    if not body.wait:
        from app.graph import helpers as H  # the row must exist BEFORE the 202 so an immediate poll never sees a 404
        await H.upsert_conversation(query_id, user.customer_id, body.channel, body.message)

        async def job():
            try:
                await _run(user, body, query_id)
            except Exception:
                pass
        t = asyncio.create_task(job())
        _bg.add(t)
        t.add_done_callback(_bg.discard)
        return JSONResponse(status_code=202, content=QueryResponse(query_id=query_id, status="processing").model_dump())
    return _resp(await _run(user, body, query_id))


@router.get("/query/{query_id}", response_model=QueryResponse)
async def get_query(query_id: str, user: User = Depends(current_user)):
    async with session_scope() as s:
        row = await s.get(m.Conversation, query_id)
    if not row or (user.role == "customer" and row.customer_id != user.customer_id):  # same 404 for foreign ids (no enumeration)
        raise HTTPException(404, "query not found")
    extra = json.loads(row.result_json) if row.result_json else {}
    return QueryResponse(query_id=row.id, status=row.status, reply=row.final_reply, intents=extra.get("intents", []),
                         review_id=extra.get("review_id"), latency_ms=row.latency_ms, flags=extra.get("flags", {}))


@router.post("/query/stream")
async def stream_query(body: QueryRequest, user: User = Depends(customer_user)):
    """Server-sent events: one event per graph node as it completes, then the final result."""
    query_id = uuid.uuid4().hex

    async def gen():
        queue: asyncio.Queue = asyncio.Queue()

        async def work():
            try:
                res = await _run(user, body, query_id)
                await queue.put(("result", res))
            except HTTPException as e:
                await queue.put(("error", {"status_code": e.status_code, "detail": e.detail}))
            except Exception as e:
                await queue.put(("error", {"detail": type(e).__name__}))
            await queue.put(None)

        from app.observability.tracing import tracer
        cb = lambda ev: queue.put_nowait(("trace", ev))   # noqa: E731  live spans / guardrail events for the console UI
        tracer.attach(query_id, cb)
        task = asyncio.create_task(work())
        yield {"event": "accepted", "data": json.dumps({"query_id": query_id})}
        seen = 0
        while True:
            try:
                item = await asyncio.wait_for(queue.get(), timeout=0.4)
            except asyncio.TimeoutError:
                st = await runner.get_state(query_id)  # progress = which pipeline stages have produced output so far
                stages = [k for k in ("input", "dispatch", "specialist_outputs", "merged", "validation") if st and st.get(k)]
                if len(stages) > seen:
                    seen = len(stages)
                    yield {"event": "progress", "data": json.dumps({"stages": stages})}
                continue
            if item is None:
                break
            kind, payload = item
            yield {"event": kind, "data": json.dumps(payload, default=str)}
        tracer.detach(query_id, cb)
        await task

    return EventSourceResponse(gen())
