# Orbit Support — a multi-agent customer-support system you can actually trust

[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) ![python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue) ![tests](https://img.shields.io/badge/tests-141%20offline%20%2B%2041%20live-brightgreen)

### 🔗 **[Live demo → orbit-support.onrender.com](https://orbit-support.onrender.com)** · [API docs](https://orbit-support.onrender.com/docs)

Pick a demo login (each has different test data: a double charge, a failed payment, a large refund…) and write anything: the agent works out the problem from your message, not from the login. Sign in as **Support staff** to approve/edit/reject the AI's drafts in the review queue. Try the **Guardrail lab** to fire real prompt-injection / SQL-injection / data-theft attempts at it.
> The demo runs on Render's free tier: the first request after idle takes ~30–60 s to wake up, and answers take ~8 s because the free NVIDIA endpoint is slow. All data is **synthetic**.

**What it is:** a dispatcher routes each message; billing / technical / general specialists and an escalation agent work in parallel with real tools (invoices, refund policy engine, logs, KB search); a validator checks every amount, id, date and claim against the evidence before the customer sees it; risky or uncertain cases pause in a human review queue and resume when staff resolve them. Built for **accuracy, guardrails and latency**, with every claim measured (see [Results](#results-at-a-glance-details-and-caveats-reportsindexmd)).

**Stack:** LangGraph · FastAPI · Pydantic · Langfuse (optional) · NVIDIA NIM (free API) · SQLite/SQLAlchemy · vanilla-JS UI. Built to be *measurably* accurate, guarded and fast, and honest about all three.

A customer message (REST or Slack) goes through input guardrails → a **dispatcher** that routes it → up to two **specialists** (billing, technical, general) and/or an **escalation** agent running **in parallel** → a **validator** (deterministic grounding checks + LLM faithfulness judge) → either the customer, a revision, or a **human review queue** where the LangGraph run is *paused* until staff resolve it. Everything is traced (local JSONL + Langfuse) with PII masked.

> The company ("Orbit"), its customers, invoices and knowledge base are **synthetic**. Nothing here is real customer data.

## Where to look (for manual checking)

| I want to… | open |
|---|---|
| see every measured number | [`reports/INDEX.md`](reports/INDEX.md) |
| know **every guardrail / method** and which test proves it | [`docs/guardrails.md`](docs/guardrails.md) |
| see **what the system is responsible for** (tasks, workflows, tools, agent boundaries, state, trust boundaries, security policies, failure & escalation behaviour) | [`docs/support-scope.md`](docs/support-scope.md), or the **Scope & design** tab in the UI |
| understand the design | [`docs/architecture.md`](docs/architecture.md) |
| read real incidents + how traces found them | [`docs/debugging-case-studies.md`](docs/debugging-case-studies.md) |
| see what went wrong in the eval, case by case | `reports/04_eval_full.md` (failures section) and `reports/eval_runs/full/results.jsonl` |
| audit the validator's decisions | `logs/validator_audit.jsonl`, `reports/eval_runs/full/logs/validator_audit.jsonl` |
| see blocked attacks | `logs/security_events.jsonl` |
| see where time went for one query | `python scripts/trace_view.py --last 3` |

## Run it locally

(Kaggle datasets used for evaluation are downloaded separately with the Kaggle CLI into `data/raw/`; the app itself and the offline tests do not need them.)

## Quick start

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # put NVIDIA_API_KEY=nvapi-... in it (never commit it)
make seed                       # synthetic backend (200 customers, 879 invoices, edge cases) + golden dataset
make test                       # 141 offline tests: unit + guardrails + graph (mock LLM) + API + Slack
make live-test                  # 41 acceptance tests against the real NVIDIA API (per agent)
make run                        # API on :8000  (docs at /docs)
```

```bash
# get a token (demo credentials are written to data/demo_credentials.json by the seed; git-ignored)
CID=CUST-000111; SECRET=$(python3 -c "import json;print(json.load(open('data/demo_credentials.json'))['$CID'])")
TOKEN=$(curl -s localhost:8000/auth/token -H 'content-type: application/json' -d "{\"client_id\":\"$CID\",\"client_secret\":\"$SECRET\"}" | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -s localhost:8000/v1/query -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{"message":"Why did my last payment fail?"}'
```

| endpoint | purpose |
|---|---|
| `POST /auth/token` | client id + secret → JWT (roles: customer, agent_staff, admin) |
| `POST /v1/query` | submit a query (`wait:false` → 202 + poll). Identity comes from the token only |
| `GET /v1/query/{id}` · `POST /v1/query/stream` | status/result · SSE progress |
| `GET /v1/review-queue` · `POST /v1/review-queue/{id}/resolve` | staff: list / approve · edit · reject (resumes the paused graph) |
| `POST /slack/events` · `/slack/interactions` | Slack Events API + review buttons (signature-verified) |
| `GET /healthz` · `/metrics` | health · Prometheus |

Staff demo logins (`staff-alice`, `admin`) are in `data/demo_credentials.json` too.

## Configuration

`.env` (see `.env.example`): `NVIDIA_API_KEY`, `JWT_SECRET`, optional `LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST` (without keys traces go to `logs/traces/*.jsonl` only), optional `SLACK_BOT_TOKEN`/`SLACK_SIGNING_SECRET` (without a token outgoing Slack messages go to `logs/slack_outbox.jsonl`). Default model: `nvidia/nemotron-3-ultra-550b-a55b` (the original `nemotron-3-super-120b` was retired by NVIDIA on 2026-10-03). Models, thresholds, rate limits and latency knobs are all in `app/core/config.py` and overridable by env var (e.g. `SPECIALIST_MODEL`, `VALIDATOR_THINKING=true`, `MERGE_MODE=llm`, `MAX_REVISIONS`, `REFUND_AUTO_LIMIT_USD`).

`LLM_MODE=mock` runs the whole system with a deterministic fake LLM (offline tests, CI, load tests).

## Evaluation methodology (how the numbers are produced)

1. **Golden dataset** — `evals/golden.jsonl`, 260 cases built by `evals/build_golden.py` from three non-LLM ground truths: the seed manifest (invoice ids, amounts, failure reasons, days since payment…), the knowledge-base pages, and Kaggle corpora (Bitext support phrasing, prompt-injection/jailbreak sets, SQLi set).
2. **Deterministic checks** — required facts (regex), forbidden claims, DB side-effects (refund created? status? ticket? review row & priority?), routing, leak patterns.
3. **Independent faithfulness judge** (`--judge`) — separate prompt, reference = the customer's real DB rows + KB chunks retrieved from the *reply*, not from the agent's evidence.
4. **Honest reporting** — failures are listed case by case in the report. Label noise in the Kaggle-derived routing sets is documented.

```bash
make eval          # live LLM, ~30 min on the free key -> reports/04_eval_full.md
make guardrails    # reports/02_guardrails.md      make load   # reports/06_load_test.md
```

## Results at a glance (details and caveats: [`reports/INDEX.md`](reports/INDEX.md))

| target from the blueprint | measured | verdict |
|---|---|---|
| 98 % accuracy | **98.1 %** on the 260-case golden set (tuned against → optimistic); **90.0 %** on a fresh 70-case hold-out, first pass (unbiased), 94.3 % after fixing what it exposed | **~90 % is the honest figure** |
| guardrails | injection / SQLi / cross-customer / refund-bypass / secrets: **100 %** safe outcomes (golden and hold-out), **0 leaks**, **0 %** false positives on 28 benign look-alikes + 600 real support messages; PII scan of logs: 0 unmasked values | met |
| <2 s latency | **p50 7.8 s, p95 20.4 s** single-user on the free API (≈4.4 LLM calls/query); input guard ~10 ms, off-topic <1 s, cache hits in ms | **not met** (provider-bound) |
| 100+ concurrent users | orchestration layer (mock LLM): 100 users with think time → p50 0.9 s / p95 3.6 s, 0 errors; 250 users OK; 20 % injected LLM failures → 0 × 5xx | met for the orchestration layer |
| 500+ queries/day | 500 queries in 51 s (mock LLM); real capacity ≈ 20–35 k/day per key | met |
| 4 h → 15 min debugging | not measured; 17 traced incidents documented | n/a |

**Quote only what the reports show.** In particular: use ≈90 % (hold-out) rather than 98 %, and do not claim sub-2-second responses.

## Known limitations

SQLite + in-process rate limiter/cache are single-process (use Postgres/Redis for multi-worker — compose file included, untested here because Docker isn't installed on the dev machine). Escalation holding replies are English. Alembic migrations are not set up (the seed rebuilds the schema). Slack/Langfuse were exercised in dry-run/offline mode (no workspace/keys supplied). Details: `docs/guardrails.md` §13.

## The agent console (web UI)

Three-pane mission control: the **customer conversation** (left), a live **agent network** (centre: agents light up, packets flow along edges, tools flash, a human desk node turns amber when it is waiting), and **system state** (right: intent, confidence, urgency, sentiment, risk, agent states). Underneath, the **live execution trace**, the **tool calls** (click for arguments and result) and the **security & policy layer** (ten checks, each pass/blocked/info as it happens). Along the bottom: **scenario buttons** (and a **Schedule** button that auto-plays chosen scenarios on an interval, in the browser tab), (duplicate payment, refund over $100, angry customer, prompt injection, someone else's data, …), **Explain this execution** (a plain-language account of what happened and why) and the **Human escalation desk** (the case exactly as the specialist sees it, with approve / edit / reject that updates the customer's chat). The **Scope & design** tab shows the support scope, workflows, tools, agent boundaries, trust boundaries, policies and failure behaviour.

It is driven by real events: `POST /v1/query/stream` emits `trace` server-sent events (agent start/end, tool calls with masked arguments, guardrail decisions, validator verdicts), never prompt text. The UI is static files in `frontend/` (no build step): hosted on **Vercel**, talking to the API on **Render** (`CORS_ORIGINS`), and also served by the API at `/app/`. `DEMO_MODE=true` shows synthetic demo logins (never with real data).

## Deploy your own

Backend: `render.yaml` is a Blueprint. Fork the repo, then in Render choose **New → Blueprint**, select the fork and set `NVIDIA_API_KEY` (and `CORS_ORIGINS` to your UI's origin). Frontend: `cd frontend && npx vercel --prod`; `frontend/config.js` points `*.vercel.app` hosts at the Render API: edit the URL there for your own backend.

## License

MIT, see [LICENSE](LICENSE). Datasets referenced for evaluation (Bitext, multilingual tickets, prompt-injection and SQLi corpora from Kaggle) keep their own licenses and are not redistributed here.
