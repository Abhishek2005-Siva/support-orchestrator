# Multi-Agent Customer Support Orchestration System — Project Blueprint

Stack: **LangGraph · FastAPI · Langfuse · Pydantic · NVIDIA NIM (free API)**

This document covers what to build, how the pieces fit, and in what order. No code yet — this is the plan.

---

## 1. What we're building (one paragraph)

A customer sends a support message (via REST API or Slack). A **dispatcher agent** classifies it and decides which specialists are needed. One or more **specialist agents** (billing, technical, escalation) work on it, calling tools (order DB, knowledge base, ticketing). A **validator agent** checks the draft answer for hallucinations, policy violations and uncertainty before anything reaches the customer. Low-confidence or risky answers go to a **human review queue** instead. Every step is traced in **Langfuse**. The whole thing is wrapped in a **FastAPI** service with JWT auth and rate limiting.

---

## 2. Swapping Claude for NVIDIA's free API

NVIDIA's hosted NIM catalog (build.nvidia.com) gives free credits/rate-limited access to open models through an **OpenAI-compatible** endpoint.

- Base URL: `https://integrate.api.nvidia.com/v1`
- Auth: `Authorization: Bearer nvapi-...` (key from build.nvidia.com; you'll provide it later)
- Two ways to call it from LangGraph:
  1. `langchain-nvidia-ai-endpoints` → `ChatNVIDIA(model=..., api_key=...)`
  2. `langchain-openai` → `ChatOpenAI(base_url="https://integrate.api.nvidia.com/v1", api_key=..., model=...)`
  
  Option 2 is more portable (swap providers by changing 3 env vars). **Recommendation: use option 2 behind a single `get_llm()` factory** so the provider is one config change.

### Model choices (verify availability on build.nvidia.com when we start — the catalog changes)
| Role | Needs | Candidate models |
|---|---|---|
| Dispatcher | fast, cheap, good at structured output | a small instruct model (e.g. Llama 3.1 8B instruct) |
| Specialists | tool/function calling, decent reasoning | Llama 3.3 70B instruct / Nemotron 70B class |
| Validator | strongest reasoning, strict JSON | the biggest model available, temperature 0 |

### Free-tier realities (important)
- **Rate limits are low** (roughly tens of requests/minute per key; check your account). This conflicts with the resume claims of "100+ concurrent users" and "<2s latency." Plan for it:
  - A global LLM semaphore + retry with exponential backoff (`tenacity`).
  - Cache repeated queries (Redis or in-memory LRU).
  - For the load-test demo, use a **mock LLM mode** (fake deterministic responses) to prove the *orchestration layer* handles 100+ concurrent users; run the real model for accuracy evals.
- Tool calling quality varies by open model. Where function-calling is flaky, fall back to **structured output via JSON schema + Pydantic parsing with a repair/retry step**.
- Models can be slower than 2s per call. A 3-stage pipeline (dispatch → specialist → validate) is ~3 LLM calls. To hit <2s p95 you'll need small models for dispatch/validate or streaming/caching. Be ready to measure and report the real number.

---

## 3. Architecture

```
                      ┌──────────────┐
 Slack / Web / REST → │   FastAPI    │  JWT auth, rate limit, Pydantic input validation
                      └──────┬───────┘
                             │ (input guardrails: PII scrub, injection check, length)
                             ▼
                   ┌───────────────────┐
                   │ Dispatcher Agent  │  intent + urgency + sentiment + which specialists
                   └─────────┬─────────┘
             fan-out (LangGraph Send API, parallel)
        ┌────────────┬───────┴────────┬──────────────┐
        ▼            ▼                ▼
 ┌────────────┐ ┌────────────┐ ┌──────────────┐
 │  Billing   │ │ Technical  │ │  Escalation  │
 │   Agent    │ │   Agent    │ │    Agent     │
 └─────┬──────┘ └─────┬──────┘ └──────┬───────┘
       └──── tools ────┴───── tools ───┘
                             │ fan-in (merge partial answers)
                             ▼
                   ┌───────────────────┐
                   │  Validator Agent  │  grounding, policy, confidence
                   └─────────┬─────────┘
              ┌──────────────┼────────────────┐
         pass │         retry (≤2x)       fail/uncertain
              ▼              ▼                 ▼
        Deliver reply   back to specialist   Human review queue
        (API / Slack)                        (+ Slack alert)

 Everything above emits Langfuse traces (one trace per query, one span per node/tool/LLM call).
```

### Clarifying the resume wording
"Dispatcher routes → 3 specialists process in parallel" — taken literally, running all three on every query wastes calls. Sensible design:
- Dispatcher returns a **list** of needed specialists. Single-intent query → 1 specialist. Multi-intent ("I was double charged and the app crashes") → 2 in parallel via LangGraph's `Send`. The escalation agent runs in parallel whenever urgency/sentiment is high.
- This still legitimately demonstrates parallel fan-out/fan-in, and it's defensible in an interview.

---

## 4. Agents in detail

### 4.1 Dispatcher Agent
- **Input:** validated customer message + conversation history + customer profile (tier, account status).
- **Output (Pydantic `DispatchDecision`):**
  - `intents: list[Literal["billing","technical","escalation","general"]]`
  - `urgency: low|medium|high|critical`
  - `sentiment: positive|neutral|negative|angry`
  - `confidence: float`
  - `reasoning: str` (short, for traces)
- **Rules:** angry sentiment or "legal/refund dispute/cancel account/data breach" keywords force `escalation` in addition to others. `general` with high confidence can be answered directly from the KB without a specialist.
- **Tools:** none (pure classification). Keep it cheap and fast.

### 4.2 Billing Agent
- Handles: invoices, charges, refunds, plan changes, payment failures.
- **Tools:**
  - `get_customer(customer_id)` — profile and plan
  - `get_invoices(customer_id, limit)` — recent invoices
  - `get_payment_status(invoice_id)`
  - `check_refund_eligibility(invoice_id)` — rules engine (deterministic, not LLM)
  - `create_refund_request(invoice_id, reason)` — **write action, gated** (see §7: requires human approval above a threshold)
  - `search_knowledge_base(query, category="billing")`
- Output: `SpecialistResponse` (draft reply, `sources`, `actions_taken`, `confidence`).

### 4.3 Technical Agent
- Handles: errors, outages, how-to, integration, bugs.
- **Tools:**
  - `search_knowledge_base(query, category="technical")` — RAG over docs/FAQ
  - `get_service_status()` — mock status page
  - `get_user_logs(customer_id, window)` — mock error logs
  - `create_ticket(summary, severity, ...)` — writes to ticket DB
  - `run_diagnostic(customer_id, check)` — simulated safe checks
- Output: same `SpecialistResponse` schema.

### 4.4 Escalation Agent
- Handles: angry/high-risk/legal/VIP/repeat-contact cases. It does **not** try to resolve; it summarizes and routes.
- **Tools:**
  - `get_ticket_history(customer_id)`
  - `summarize_conversation()` — context package for the human
  - `assign_to_human(queue, priority, summary)` — writes to review queue + Slack notification
  - `send_holding_reply()` — empathetic "a human is on it, ETA X" message
- Output: escalation record + holding message.

### 4.5 Validator Agent
Runs on every candidate response, in two layers:

1. **Deterministic checks (no LLM, fast):**
   - No PII leakage (other customers' emails, card numbers) — regex/Presidio
   - No forbidden promises (e.g. "I guarantee a refund") — rule list
   - Every factual claim about the account must trace to a tool result (`sources` non-empty when numbers/dates/amounts appear)
   - Amounts/dates in the reply match those in tool outputs (string/number cross-check)
2. **LLM-as-judge check:**
   - Is the answer grounded in the supplied tool outputs/KB passages? (faithfulness)
   - Does it actually answer the question? Is tone appropriate?
   - Returns `ValidationResult { verdict: approve|revise|human_review, issues: [...], confidence: float }`
- **Routing:** `approve` → deliver. `revise` → back to specialist with the issue list (max 2 loops). `human_review` or confidence < threshold (e.g. 0.7) → human queue.

This is what makes the "flags uncertain responses for human review" and "prevents hallucinations" claims real.

---

## 5. LangGraph orchestration

### Shared state (`TypedDict` / Pydantic)
```
SupportState:
  query_id, customer_id, channel, message, history
  dispatch: DispatchDecision
  specialist_outputs: Annotated[list[SpecialistResponse], operator.add]   # reducer enables parallel merge
  merged_draft: str
  validation: ValidationResult
  retry_count: int
  final_reply, status: delivered | human_review | rejected
```

### Nodes
`input_guard` → `dispatcher` → (conditional fan-out) → `billing` | `technical` | `escalation` → `merge` → `validator` → (conditional) → `deliver` | `revise` (loops to specialists) | `human_review`

### Key LangGraph features to use
- **Conditional edges** for routing (dispatcher → specialists, validator → deliver/revise/human).
- **`Send` API** for dynamic parallel fan-out.
- **Reducers** on `specialist_outputs` to merge parallel results.
- **Specialists as ReAct sub-graphs** (`create_react_agent` with tools) — each is its own small graph with a tool-calling loop and a max-iteration cap (e.g. 5).
- **Checkpointer** (SQLite for dev, Postgres for prod) so conversations resume and human-review can pause/resume the graph with `interrupt()`.
- **Merge node:** if multiple specialists answered, a small LLM call (or template) combines into one coherent reply; escalation's holding message is prepended if present.

### Error handling
- Per-node timeout, per-LLM-call retry with backoff, max loop count so the graph can never spin forever.
- Any unhandled failure → safe fallback: human review queue + apologetic holding reply.

---

## 6. Tools & data layer

Since there's no real company backend, **build a realistic mock backend** — this is also the "SQL injection" surface the resume refers to.

- **Database:** SQLite (dev) → PostgreSQL (docker-compose). Tables: `customers`, `invoices`, `payments`, `tickets`, `human_review_queue`, `audit_log`, `conversations`.
- **Seed data:** script generating ~200 fake customers, ~1000 invoices, with deliberate edge cases (double charges, failed payments, expired cards).
- **Knowledge base:** 30–60 markdown FAQ/doc pages → chunk → embed → vector store (Chroma/FAISS locally). NVIDIA also offers a free embedding model (e.g. `nvidia/nv-embedqa-e5-v5`), so embeddings can stay on the same free key.
- **Tool design rules:**
  - Every tool has a Pydantic args schema (strict types, length limits, regex for IDs like `^CUST-\d{6}$`).
  - Tools use **parameterized queries only** (SQLAlchemy/`?` placeholders) — never string-formatted SQL.
  - Tools get `customer_id` **injected from the authenticated session, not from the LLM**, so an agent can never read another customer's data even if prompt-injected.
  - Read tools are open; write tools (refund, ticket, assign) are logged to `audit_log` and some require approval.

---

## 7. Guardrails (the part that backs your resume claims)

### Input guardrails (before any LLM)
- Pydantic request model: max length, strip control chars, valid channel enum.
- Prompt-injection heuristics (“ignore previous instructions”, role-play attempts, system-prompt extraction) → flag/refuse. Optionally an LLM classifier.
- PII scrubbing in logs/traces (mask card numbers, emails) before sending to Langfuse.

### Tool-call guardrails
- Schema validation on every tool arg (Pydantic).
- **SQL injection blocking, two layers:** (1) parameterized queries so injection is structurally impossible, (2) a pre-execution check that rejects args containing SQL meta-patterns (`;`, `--`, `UNION SELECT`, `DROP`, etc.) and logs the attempt as a security event. Write tests with a corpus of injection payloads.
- Allow-list of tools per agent (billing agent can't call `assign_to_human`, etc.).
- Authorization check: tool's `customer_id` must equal session's.
- Write-action limits: refund > $X → forced human approval.

### Output guardrails (validator, §4.5)
- Grounding, PII, forbidden-promise, tone, confidence threshold → human review.

---

## 8. FastAPI service

### Endpoints
| Method | Path | Purpose |
|---|---|---|
| POST | `/auth/token` | issue JWT (mock user store; or API-key → JWT) |
| POST | `/v1/query` | submit a support query, returns reply or `status: human_review` |
| POST | `/v1/query/stream` | SSE streaming of agent progress (optional) |
| GET | `/v1/query/{id}` | fetch status/result of async job |
| GET | `/v1/review-queue` | (staff role) list pending human reviews |
| POST | `/v1/review-queue/{id}/resolve` | human approves/edits/rejects |
| POST | `/slack/events` | Slack Events API webhook (signature-verified) |
| GET | `/healthz`, `/metrics` | health + Prometheus metrics |

### Security & infra
- **JWT auth:** `python-jose`/`pyjwt`, short expiry, roles (`customer`, `agent_staff`, `admin`), FastAPI `Depends` guards.
- **Rate limiting:** 100 req/min per user/key — `slowapi` (in-memory) or Redis-backed sliding window for multi-worker correctness. Return `429` + `Retry-After`.
- **Concurrency:** fully `async` (httpx/async LLM client, async DB driver `asyncpg`/`aiosqlite`), uvicorn with multiple workers behind gunicorn. Use a bounded semaphore around LLM calls (free-tier-friendly).
- **Config:** `pydantic-settings` reading `.env` (NVIDIA key, JWT secret, Slack tokens, Langfuse keys).
- **Docker:** `Dockerfile` + `docker-compose.yml` (api, postgres, redis, optionally self-hosted Langfuse).

### Slack integration
- Create a Slack app (free workspace) with `app_mentions:read`, `chat:write`, `im:history`, `channels:history`.
- Use Events API (HTTPS webhook; use `ngrok`/cloudflared in dev) or **Socket Mode** (easier, no public URL).
- Flow: Slack message → map Slack user to customer → run graph → reply in thread. Human-review items also post to a `#support-escalations` channel with Approve/Edit buttons (Block Kit interactivity).
- Verify Slack request signatures; respond within 3s and process async.
- Library: `slack-bolt`.

---

## 9. Observability — Langfuse

- **Setup:** Langfuse Cloud free tier, or self-host with docker-compose. Python SDK + LangChain/LangGraph `CallbackHandler`.
- **Trace structure:** 1 trace per query (`trace_id` = `query_id`), tagged with `customer_id` (hashed), `channel`, `intent`. Each LangGraph node = span; each LLM call = generation (with token counts, latency, model); each tool call = span with args/result (PII-masked).
- **Scores:** attach validator confidence, `human_review_flag`, user thumbs up/down, and eval scores to traces.
- **Dashboards/queries to build:** p50/p95 latency per node, error rate per tool, human-review rate, token usage, injection attempts.
- **The "4 hours → 15 min" claim:** to make it real, keep a log of seeded failure scenarios (e.g. wrong tool arg, stale KB, validator false negative) and measure time to root-cause **with** traces vs. digging through plain logs. Document 3–5 real incidents in a `docs/debugging-case-studies.md`. Then the number is yours and defensible.

---

## 10. Evaluation — making "98% accuracy" real

You need a labeled eval set, otherwise the number is unsupported.
1. Build a **golden dataset** of 150–300 support queries with expected: intent, correct specialist(s), key facts that must appear, and whether it should escalate. (Generate drafts with the LLM, then hand-review.)
2. Metrics:
   - Routing accuracy (dispatcher vs label)
   - Answer correctness (rubric-based LLM judge + exact fact checks)
   - Hallucination rate (claims not grounded in tool output)
   - Correct-escalation precision/recall
   - Guardrail tests: SQLi corpus block rate, prompt-injection block rate
3. Run evals in CI (`pytest` + `promptfoo` or `ragas`/custom), store results as Langfuse **datasets/experiments**.
4. **Report whatever number you actually measure.** If it's 91% on the free open model, say 91% — a true number you can explain beats a claimed 98% you can't reproduce. Once you have real results, update the resume bullets to match.

Same for "500+ daily queries" and "100+ concurrent users": run a **load test** (Locust or k6) and a **traffic simulator** script that replays the golden set at that rate, then quote what the tests show (see mock-LLM note in §2).

---

## 11. Project structure

```
support-orchestrator/
├── app/
│   ├── main.py                 # FastAPI app factory
│   ├── api/                    # routers: auth, query, review, slack, health
│   ├── core/                   # config, security (JWT), rate limit, logging
│   ├── agents/
│   │   ├── dispatcher.py
│   │   ├── billing.py
│   │   ├── technical.py
│   │   ├── escalation.py
│   │   └── validator.py
│   ├── graph/                  # state.py, builder.py, routing.py
│   ├── tools/                  # billing_tools.py, tech_tools.py, escalation_tools.py, kb.py
│   ├── guardrails/             # input.py, tool_args.py, output.py, pii.py, sqli.py
│   ├── llm/                    # get_llm() factory, nvidia client, retry/semaphore, mock LLM
│   ├── db/                     # models, session, migrations (alembic), seed.py
│   ├── integrations/           # slack.py
│   ├── observability/          # langfuse setup, masking
│   └── schemas/                # pydantic models (requests, DispatchDecision, ...)
├── kb/                         # markdown knowledge-base docs
├── evals/                      # golden dataset, eval runner, security corpus
├── tests/                      # unit + integration + guardrail tests
├── loadtests/                  # locustfile.py
├── docs/                       # architecture diagram, case studies, results
├── docker-compose.yml
├── Dockerfile
├── pyproject.toml
├── .env.example
└── README.md
```

---

## 12. Prerequisites

### Knowledge
- Python 3.11+ async/await, type hints
- Pydantic v2
- FastAPI basics (routers, `Depends`, middleware)
- LangChain basics (messages, tools) then LangGraph (state, nodes, edges, `Send`, checkpointers)
- SQL basics + SQLAlchemy
- Basic RAG concepts (chunking, embeddings, retrieval)
- JWT/auth concepts, HTTP rate limiting

### Accounts / keys (all free)
- **NVIDIA build.nvidia.com** account → API key (`nvapi-...`) *(you'll provide later)*
- **Langfuse** Cloud account (free tier) → public/secret keys, or self-host
- **Slack** free workspace + Slack app (bot token, signing secret, app token if Socket Mode)
- **GitHub** repo (CI with GitHub Actions)
- Optional: ngrok account (for Slack webhooks in dev)

### Local tooling
- Python 3.11/3.12, `uv` or `poetry` (dependency mgmt)
- Docker + Docker Compose
- PostgreSQL & Redis (via Docker)
- `git`, `make`
- Locust or k6 for load testing

### Key Python packages
`fastapi`, `uvicorn[standard]`, `gunicorn`, `pydantic`, `pydantic-settings`, `langgraph`, `langchain-core`, `langchain-openai` (or `langchain-nvidia-ai-endpoints`), `langfuse`, `sqlalchemy`, `alembic`, `asyncpg`/`aiosqlite`, `redis`, `slowapi`, `pyjwt`, `passlib`, `slack-bolt`, `chromadb` (or `faiss-cpu`), `tenacity`, `httpx`, `presidio-analyzer` (optional), `pytest`, `pytest-asyncio`, `locust`, `structlog`.

---

## 13. Build roadmap (suggested order)

| Phase | Deliverable | Est. |
|---|---|---|
| 0 | Repo, env, `.env`, `get_llm()` factory, sanity call to NVIDIA API, Langfuse "hello trace" | 0.5 day |
| 1 | DB schema + seed data + KB ingestion (RAG) | 1–2 days |
| 2 | Tools with Pydantic schemas + parameterized queries + unit tests | 1–2 days |
| 3 | Single specialist (Technical) as a ReAct agent with Langfuse tracing | 1 day |
| 4 | Dispatcher + all 3 specialists + LangGraph routing / parallel fan-out | 2 days |
| 5 | Validator agent + retry loop + human-review queue + checkpointer/`interrupt` | 2 days |
| 6 | Guardrails (input, tool-arg, SQLi corpus, PII masking) + tests | 1–2 days |
| 7 | FastAPI service: JWT, rate limiting, endpoints, async | 2 days |
| 8 | Slack integration | 1 day |
| 9 | Golden dataset + eval runner + Langfuse experiments | 2 days |
| 10 | Load tests, perf tuning (caching, semaphores), debugging case studies | 1–2 days |
| 11 | Docker-compose, README, architecture diagram, demo video | 1 day |

Roughly 2–3 weeks part-time. Build **vertically** (get one query working end-to-end through Phase 5 before polishing) rather than perfecting each layer.

---

## 14. Risks & decisions to settle before coding

1. **Free-tier rate limits** — decide on mock-LLM mode for load tests (recommended) and a caching strategy.
2. **Tool-calling reliability of open models** — test 2–3 NVIDIA models early (Phase 0) on a tool-calling task and pick per agent role; keep JSON-schema fallback.
3. **Latency target (<2s)** — likely needs small models for dispatcher/validator + parallelism; measure early.
4. **Mock vs. real data** — we'll fabricate the company backend; be upfront about that in the README (it's a demo system on synthetic data).
5. **Resume numbers** — treat 98% / 500+ / 100+ / 4h→15min as *targets to measure*, not facts, and update the resume to match your measured results.
6. **Human-in-the-loop UX** — minimal: Slack buttons + a simple `/review-queue` API. A web UI is optional stretch.

---

## 15. Stretch goals
- Streaming responses (SSE) showing agent progress
- Conversation memory/long-term customer context
- Semantic cache for repeated questions
- A tiny React/HTMX admin dashboard for the review queue
- Multi-language support
- Model fallback chain (primary NVIDIA model → secondary if rate-limited)

---

## 16. What I need from you to start
1. Your NVIDIA API key (put it in `.env` as `NVIDIA_API_KEY`, never commit it).
2. Langfuse keys (or I'll set up self-host in docker-compose).
3. Slack workspace/app details — or we defer Slack to Phase 8.
4. Confirmation: Postgres via Docker, or keep SQLite for simplicity at first?
