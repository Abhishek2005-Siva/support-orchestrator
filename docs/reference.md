# Reference: run, configure, evaluate, deploy

The short overview is in the [README](../README.md). This page holds the details.

## Run it locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env            # put NVIDIA_API_KEY=nvapi-... in it (never commit it)
make seed                       # simulated bank (300 customers, ~13.5k ledger rows, policies, knowledge graph) + golden dataset
make test                       # 181 offline tests: unit + guardrails + graph (mock LLM) + API + Slack
make live-test                  # 45 acceptance tests against the real NVIDIA API (per agent)
make run                        # API + console on :8000  (docs at /docs, UI at /app/)
```

Kaggle datasets used for evaluation are downloaded separately with the Kaggle CLI into `data/raw/`. The app and the offline tests do not need them.

```bash
# get a token (demo credentials are written to data/demo_credentials.json by the seed; git-ignored)
CID=CUST-000111; SECRET=$(python3 -c "import json;print(json.load(open('data/demo_credentials.json'))['$CID'])")
TOKEN=$(curl -s localhost:8000/auth/token -H 'content-type: application/json' -d "{\"client_id\":\"$CID\",\"client_secret\":\"$SECRET\"}" | python3 -c "import sys,json;print(json.load(sys.stdin)['access_token'])")
curl -s localhost:8000/v1/query -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' -d '{"message":"Why did my last payment fail?"}'
```

## API

| endpoint | purpose |
|---|---|
| `POST /auth/token` | client id + secret → JWT (roles: customer, agent_staff, admin) |
| `POST /v1/query` | submit a query (`wait:false` → 202 + poll). Identity comes from the token only |
| `GET /v1/query/{id}` · `POST /v1/query/stream` | status/result · SSE progress and live `trace` events |
| `GET /v1/review-queue` · `POST /v1/review-queue/{id}/resolve` | staff: list / approve · edit · reject (resumes the paused graph) |
| `POST /slack/events` · `/slack/interactions` | Slack Events API + review buttons (signature-verified) |
| `GET /v1/data/overview` · `/table/{name}` · `/policies` · `/graph` · `/verifications` | scoped read-only views of the bank tables, policies, knowledge graph and verification records (customers see only their own rows; internal columns never exposed) |
| `GET /demo/accounts` | synthetic demo logins (only when `DEMO_MODE=true`) |
| `GET /healthz` · `/metrics` | health · Prometheus |

Staff demo logins (`staff-alice`, `admin`) are in `data/demo_credentials.json` too.

## Configuration

`.env` (see `.env.example`): `NVIDIA_API_KEY`, `JWT_SECRET`, optional `LANGFUSE_PUBLIC_KEY/SECRET_KEY/HOST` (without keys traces go to `logs/traces/*.jsonl` only), optional `SLACK_BOT_TOKEN`/`SLACK_SIGNING_SECRET` (without a token outgoing Slack messages go to `logs/slack_outbox.jsonl`).

Default model: `nvidia/nemotron-3-ultra-550b-a55b` (the original `nemotron-3-super-120b` was retired by NVIDIA on 2026-10-03). Models, thresholds, rate limits and latency knobs are all in `app/core/config.py` and overridable by env var (for example `SPECIALIST_MODEL`, `VALIDATOR_THINKING=true`, `MERGE_MODE=llm`, `MAX_REVISIONS`, `REFUND_AUTO_LIMIT_USD`).

`LLM_MODE=mock` runs the whole system with a deterministic fake LLM (offline tests, CI, load tests).

## Evaluation methodology

1. **Golden set**: `evals/golden.jsonl`, 254 cases built by `evals/build_golden.py` from non-LLM ground truths: the seed manifest (transaction, transfer and card ids, amounts, merchants, expected decision), the policy table and help articles, and attack corpora (prompt-injection and SQLi sets from Kaggle).
2. **Hold-out set**: `evals/holdout.jsonl`, 81 cases (different customers and phrasing, plus real PolyAI Banking77 messages) written *after* the golden set was frozen and never tuned against. Its first-pass result is the unbiased figure.
3. **Deterministic checks**: required facts (regex), forbidden claims, DB side effects (dispute filed and its status? provisional credit? card blocked? fee waived? transfer cancelled? review row and priority?), routing, leak patterns.
4. **Independent faithfulness judge** (`--judge`): separate prompt; the reference is the customer's real DB rows plus KB chunks retrieved from the *reply*, not from the agent's evidence.
5. **Honest reporting**: failures are listed case by case. Label noise in the Kaggle-derived routing sets is documented.

```bash
make eval          # live LLM, ~30 min on the free key -> reports/04_eval_full.md
make guardrails    # reports/02_guardrails.md      make load   # reports/06_load_test.md
```

Every number and how far to trust it: [`reports/INDEX.md`](../reports/INDEX.md).

## Known limitations

- SQLite, the in-process rate limiter and the answer cache are single-process. Use Postgres and Redis for several workers (a compose file is included, untested here because Docker isn't installed on the dev machine).
- No real payment rail or core-banking system sits behind the tools: a provisional credit or fee reversal is a ledger entry in a synthetic database. No SLA timers on pending reviews. Only duplicate and unauthorised disputes are automated.
- Escalation holding replies are English only.
- Alembic migrations are not set up (the seed rebuilds the schema).
- Slack and Langfuse were exercised in dry-run/offline mode (no workspace or keys supplied).
- The judge and the agents share a model family; deterministic and DB-state checks carry the action and escalation numbers.
- More: `docs/guardrails.md` §13.

## Deploy your own

- **Backend (Render):** `render.yaml` is a Blueprint. Fork the repo, then in Render choose New → Blueprint, select the fork and set `NVIDIA_API_KEY` and `CORS_ORIGINS` (your UI's origin). Free tier: sleeps when idle, ephemeral disk, re-seeds at start.
- **Frontend (Vercel):** `cd frontend && npx vercel --prod`. `frontend/config.js` points `*.vercel.app` hosts at the Render API; edit the URL there for your own backend.
- `DEMO_MODE=true` shows synthetic demo logins. Never enable it with real data.

## Repository map

See [architecture.md](architecture.md#repository-map).
