"""FastAPI app factory. Run: uvicorn app.main:app --port 8000"""
from __future__ import annotations

import logging
import time
from pathlib import Path
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.api import auth, data, demo, health, query, review
from app.core import metrics
from app.core.config import get_settings
from app.core.deps import configure_limits
from app.db.session import init_db
from app.graph import runner
from app.integrations import slack
from app.observability.tracing import tracer

MAX_BODY = 16 * 1024  # G-API-05: reject oversized bodies before parsing
log = logging.getLogger("support")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = get_settings()
    configure_limits()
    if len(s.jwt_secret) < 32 or s.jwt_secret in ("dev-secret", "change-me"):
        log.warning("JWT_SECRET is weak/default: set a random >=32-char value before exposing this service")
    await init_db()
    saver_cm = None
    if getattr(app.state, "use_sqlite_checkpointer", True):
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        saver_cm = AsyncSqliteSaver.from_conn_string(s.checkpoint_db)
        saver = await saver_cm.__aenter__()
        runner.init_graph(saver)
    else:
        runner.init_graph()
    try:
        from app.tools.kb import get_kb
        await get_kb()
    except Exception as e:  # KB degrades to BM25 / empty; the service must still start
        log.warning("KB load failed: %s", e)
    yield
    tracer.flush()
    if saver_cm:
        await saver_cm.__aexit__(None, None, None)


def create_app(*, use_sqlite_checkpointer: bool = True) -> FastAPI:
    app = FastAPI(title="Support Orchestrator", version="0.1.0", lifespan=lifespan)
    app.state.use_sqlite_checkpointer = use_sqlite_checkpointer

    @app.middleware("http")
    async def mw(request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_BODY:
            return JSONResponse({"detail": "request body too large"}, status_code=413)
        t0 = time.perf_counter()
        try:
            resp = await call_next(request)
        except Exception:
            log.exception("unhandled error rid=%s", rid)
            resp = JSONResponse({"detail": "internal error", "request_id": rid}, status_code=500)  # never leak internals
        resp.headers["X-Request-ID"] = rid
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Cache-Control"] = "no-store"
        route = request.scope.get("route")
        metrics.REQS.labels(getattr(route, "path", request.url.path), str(resp.status_code)).inc()
        resp.headers["Server-Timing"] = f"app;dur={(time.perf_counter() - t0) * 1000:.0f}"
        return resp

    s = get_settings()
    if s.cors_origins:
        from fastapi.middleware.cors import CORSMiddleware
        app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in s.cors_origins.split(",") if o.strip()], allow_methods=["GET", "POST"],
                           allow_headers=["authorization", "content-type"], expose_headers=["Retry-After", "X-Request-ID"])
    for r in (auth.router, query.router, review.router, data.router, health.router, slack.router, demo.router):
        app.include_router(r)
    from fastapi.responses import RedirectResponse
    from fastapi.staticfiles import StaticFiles
    fe = Path(__file__).resolve().parents[1] / "frontend"
    if fe.exists():  # the UI is served by the API itself: same origin, no CORS, one deploy
        app.mount("/app", StaticFiles(directory=fe, html=True), name="frontend")

        @app.get("/", include_in_schema=False)
        async def root():
            return RedirectResponse("/app/")
    return app


app = create_app()
