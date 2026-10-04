# Architecture

## One query, end to end

```
POST /v1/query  (JWT → customer_id comes from the token, never the body)
   │  G-API: auth · role · rate-limit · size · schema
   ▼
┌──────────────────────────── LangGraph (app/graph/builder.py) ────────────────────────────┐
│ intake ── input guard ‖ profile prefetch ‖ open-ticket count   (asyncio.gather)            │
│   ├─ blocked ──────────────────────────────────────────────► finalize_rejected ─► END      │
│   ├─ answer-cache hit (KB-only, validated before) ─────────► serve_cache ───────► END      │
│   └─ ok ─┬─► dispatcher   (LLM + deterministic escalation overlay)                         │
│          ├─► safety_check (NVIDIA content-safety model, best-effort, parallel)             │
│          └─► kb_warm      (embeds the message while the dispatcher runs)                   │
│                    └────────── join ─► triage                                              │
│   triage ─ off_topic ─► off_topic_reply ─► END                                             │
│          └ Send() fan-out (parallel) ─► specialist(billing|technical|general) ×0-2         │
│                                      └► escalation_node (when triggered)                 │
│   merge  (template; escalation holding text first)                                         │
│   validator ─ approve ─► deliver ─► END   (files an approval review for refunds > $100)    │
│             ├ revise (≤1) ─► specialists again with feedback ─► merge ─► validator         │
│             └ human_review ─► human_prepare ─► human_wait [interrupt, checkpointed]        │
│                                   ▲ staff approve/edit/reject (API or Slack) ─► human_resolve ─► END │
└───────────────────────────────────────────────────────────────────────────────────────────┘
   runner: timeout (60 s) · safe fallback (any failure → human queue + holding reply) · persist · traces · Slack alert
```

Specialists are **ReAct sub-graphs** (`app/agents/react.py`): `llm ⇄ tools` loop, parallel tool execution, iteration cap, prefetch stage, offered-tools-only, final JSON validated with a repair turn.

## Agents

| agent | LLM calls | tools | notes |
|---|---|---|---|
| dispatcher | 1 (JSON, few-shot) | none | intents ∈ {billing, technical, general, escalation, off_topic}, `refund_requested`, per-intent `sub_questions`; deterministic escalation triggers override it |
| billing | 1–2 | `get_invoices`, `get_payment_status`, `check_refund_eligibility`, `create_refund_request` (gated), `search_knowledge_base` | refund policy is a deterministic engine; writes are intent-gated + idempotent |
| technical | 1–2 | `search_knowledge_base`, `get_service_status`, `get_user_logs`, `run_diagnostic`, `create_ticket` (gated) | prefetches KB + logs + status in parallel |
| general | 1 | `search_knowledge_base` | KB-only; answers cacheable |
| escalation | 1 (internal note only) | `get_ticket_history`, `assign_to_human` | customer reply is a template |
| validator | 0–1 | — | deterministic checks, then LLM judge |

## Data model (SQLite → Postgres)

`customers · invoices · payments · refund_requests · tickets · error_logs · service_components` (mock company backend, synthetic) and
`human_review_queue · audit_log · security_events · conversations · api_credentials · id_sequences · meta` (system).
Seed: 200 customers, 879 invoices, deliberate edge cases (double charges, failed/expired cards, refund windows, suspended accounts, repeat contacts, error-log scenarios) with a ground-truth manifest `data/seed_manifest.json` that the evaluation uses.

## Models (measured, see `reports/00_llm_sanity.md`, `03_dispatcher*.md`)

NVIDIA's catalog lists ~80 models but only a subset is *served* on a free key (most 404, several time out). Development started on `nvidia/nemotron-3-super-120b-a12b` (fastest reliable strong model: ≈1–2 s/call, tools + JSON mode OK); **it was retired on 2026-10-03 (HTTP 410)**, so the system now runs on `nvidia/nemotron-3-ultra-550b-a55b` for dispatcher, specialists, validator, merge and judge (≈0.5–4 s/call, ~25 % transient 503s absorbed by retries), `enable_thinking=false`, with fallbacks `meta/llama-3.2-11b-vision-instruct` → `openai/gpt-oss-20b`. Dispatcher model comparison done on the retired model: `llama-3.2-11b` 89.8 % vs 92.5 %, `nemotron-3.5-lightning-30b` 88.2 % with 50 s tails (`reports/03_dispatcher*.md`). Embeddings: `nvidia/nemotron-3-embed-1b` (2048-d). Safety: `nvidia/nemotron-3.5-content-safety`. Everything is per-role configurable (`app/core/config.py`) and falls back to the next model in the chain.

## Key design decisions

1. **Policy as code, language as LLM.** Anything with a right answer (refund eligibility, priority, ids) is deterministic and unit-tested; the LLM only phrases and routes.
2. **The model never chooses identity or authority.** `customer_id` is injected from the session; writes require the customer's own intent; tools are allow-listed per agent.
3. **Fail closed on quality, fail open on extras.** Validator unreachable ⇒ human review; safety model unreachable ⇒ continue (other layers exist).
4. **Humans are part of the graph.** `interrupt()` + checkpointer, not a side channel.
5. **Observability is not optional.** Every node/LLM/tool/guardrail is a span; PII masked at the sink.
6. **Mock LLM for everything that isn't about model quality** (CI, load tests, graph logic), live LLM for everything that is.

## Repository map

```
app/main.py                 FastAPI factory (lifespan: DB, checkpointer, KB, graph)
app/api/                    auth · query (sync/async/SSE) · review · health/metrics
app/core/                   config · security (JWT, PBKDF2) · deps (auth, rate limit) · ratelimit · metrics
app/graph/                  state · nodes · builder · runner · cache · helpers
app/agents/                 dispatcher · specialists · react · escalation · validator · prompts · rules
app/tools/                  runtime (guard pipeline) · handlers (12 tools) · rules (refund engine) · kb (hybrid retrieval)
app/guardrails/             input · pii · sqli · output · events
app/llm/                    gateway (NVIDIA, retries, hedging, cache) · mock · structured (JSON+repair)
app/observability/          tracing (JSONL + Langfuse)
app/integrations/slack.py   events, signature, Block Kit review, interactions
app/db/                     models · session · seed · ids
kb/                         43 markdown help-center pages (policy ground truth)
evals/                      golden dataset builder · end-to-end runner · dispatcher/KB/guardrail evals · security corpus
loadtests/                  traffic simulator (uvicorn + mock) · locustfile
tests/                      offline suite (145) + tests/live (42, real LLM)
reports/                    numbered reports for you to read (see reports/INDEX.md)
docs/                       this file · guardrails.md · debugging-case-studies.md · blueprint.md
```
