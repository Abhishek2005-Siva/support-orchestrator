# Build progress log (newest at bottom)
Chronological record of what was built, how it was tested, and what was learned. Detailed measurements are in the numbered reports in this folder.

## Phase 0 — foundation  ✅
- NVIDIA key verified (81 models listed; only a subset actually served on this free key). `.env` created (git-ignored).
- LLM gateway (`app/llm/gateway.py`): semaphore, token bucket, retry/backoff, fallback chain, LRU cache, thinking toggle, tracing.
- Measured: `nvidia/nemotron-3-super-120b-a12b` is the best model that is reliably served (≈1-2 s/call, tool calling OK). `enable_thinking=false` is ~3x faster.
- Measured rate limit: ≈100 rpm at concurrency 4; above that mostly 429 → defaults 70 rpm / concurrency 4. Report: `00_llm_sanity.md`.
- Tracing: local JSONL (always) + Langfuse mirror (when keys exist). PII masked before write. View with `python scripts/trace_view.py`.

## Phase 1 — data + knowledge base  ✅
- SQLite schema, deterministic seed (200 customers / 879 invoices / edge-case scenarios + `data/seed_manifest.json` ground truth).
- 43-page KB; hybrid retrieval chosen by experiment (`01_kb_retrieval.md`): dense-led weighted fusion beats BM25 and RRF.

## Phase 2 — tools + guardrails  ✅
- 12 tools with strict Pydantic schemas; guarded runtime (allow-list, identity injection, SQLi scan, budget, audit). 21 unit tests.
- Input guard: SQLi + prompt-injection detectors measured on Kaggle corpora (see `02_guardrails_static.md` when generated).

## Phase 3 — specialists
- ReAct engine (LangGraph sub-graph) + parallel tool prefetch + hedged requests (LLM tail latency ~3% of calls stall 20-30 s).
- Technical agent: 8/8 live tests pass. Billing agent: 12/12 live tests pass (DB-state assertions).
  - Found + fixed by tests: model skipped a refunded newest invoice ("last invoice" rule), unnecessary ticket creation, search loops (per-tool cap).
  - Escalation agent 7/7 live tests; Dispatcher 92.5 % exact-set routing on 186 labelled cases (`03_dispatcher.md`); chosen over llama-3.2-11b (89.8 %) and nemotron-lightning-30b (88.2 %).
  - Validator (deterministic grounding checks + LLM faithfulness judge) 14 offline + 8 live tests.

## Phase 4-5 — graph, human-in-the-loop  ✅
- LangGraph: parallel `Send` fan-out/fan-in, join of dispatcher ‖ safety ‖ KB-warm, revise loop (≤1), `interrupt()` pause + resume for human review, safe fallback, answer cache.
- 17 offline graph tests with the deterministic mock LLM (routing, guardrails, revise loop, HITL approve/edit/reject, fallback, cache).
- Bugs found by end-to-end runs and fixed (details in `docs/debugging-case-studies.md`): judge false-rejects (evidence truncation, profile missing), cross-topic hallucination, unrequested refund/ticket (→ intent-gated writes), confident answer to unanswerable question, search loops, **duplicate-id race under concurrency**, async-mode 202→404 race.

## Phase 6-8 — guardrails, API, Slack  ✅
- Input / tool / output guardrail catalog: `docs/guardrails.md` (IDs G-IN, G-TOOL, G-OUT, G-API ...). Guardrail measurements on Kaggle corpora: `02_guardrails.md`.
- FastAPI: JWT + roles, rate limit, lockout, SSE, async mode, staff review queue, Prometheus. Slack: signature, dedupe, linked users, Block Kit review buttons (dry-run outbox without token). 23 API/Slack tests.

## Phase 9-11 — evals, load, docs
- Golden dataset (260 cases): `evals/golden.jsonl`; runner `evals/run_eval.py`; load tooling `loadtests/`; docs and Dockerfile/compose/Makefile.

## Final state (2026-10-04)
- All phases complete. Offline suite: 141 tests; live per-agent suite: 41 tests (pass on the new primary model).
- Golden eval 98.1 % (tuned-against, optimistic); hold-out first pass 90.0 % (unbiased) → fixes → 94.3 %; single-user latency p50 7.8 s / p95 20.4 s; load test and chaos test in `06_load_test.md`.
- Mid-project incident: primary model retired (HTTP 410) → circuit breaker, new primary, validator never uses weak fallbacks. PII leak path through an LLM summary found by the log scan and fixed. See `docs/debugging-case-studies.md` (#1–#17).
- Not done / not possible here: real Langfuse and Slack workspaces (offline/dry-run modes tested), Docker (not installed), Alembic migrations, multi-worker Redis state.
