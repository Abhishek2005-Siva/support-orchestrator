from fastapi import APIRouter, Response
from sqlalchemy import func, select, text

from app.core import metrics
from app.db import models as m
from app.db.session import session_scope
from app.graph.cache import answer_cache
from app.llm.gateway import get_gateway

router = APIRouter(tags=["ops"])


@router.get("/healthz")
async def healthz():
    out = {"status": "ok", "db": "ok"}
    try:
        async with session_scope() as s:
            await s.execute(text("select 1"))
    except Exception as e:
        out.update(status="degraded", db=f"error: {type(e).__name__}")
    return out


@router.get("/metrics")
async def prom():
    gw = get_gateway()
    for k, v in getattr(gw, "stats", {}).items():
        metrics.LLM_CALLS.labels(k).set(v)
    for k, v in answer_cache.stats().items():
        metrics.CACHE.labels(k).set(v)
    try:
        async with session_scope() as s:
            n = (await s.execute(select(func.count()).select_from(m.HumanReview).where(m.HumanReview.status == "pending"))).scalar_one()
        metrics.REVIEW_PENDING.set(n)
    except Exception:
        pass
    return Response(metrics.render(), media_type="text/plain; version=0.0.4")
