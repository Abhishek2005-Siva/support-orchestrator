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
│          ├─► safety_check (content-safety model: advisory for confident banking requests)  │
│          └─► kb_warm      (embeds the message while the dispatcher runs)                   │
│                    └────────── join ─► triage                                              │
│   triage ─ off_topic ─► off_topic_reply ─► END                                             │
│          └ Send() fan-out (parallel) ─► specialist(payments|cards|general) ×0-2            │
│                                      └► escalation_node (when triggered)                 │
│      each specialist:  prefetch (ledger / cards / transfers + KB)                          │
│                        → VERIFY  (knowledge graph → checks → ledger + policy table)        │
│                        → ACT     (only if the verification allows it AND the customer asked)│
│                        → ReAct answer (explains the outcome)                               │
│   merge  (template; escalation holding text first)                                         │
│   validator ─ approve ─► deliver ─► END   (files an approval review for pending disputes)  │
│             ├ revise (≤1, with the actions already done carried forward) ─► specialists ─► │
│             └ human_review ─► human_prepare ─► human_wait [interrupt, checkpointed]        │
│                                   ▲ staff approve/edit/reject (API or Slack) ─► human_resolve ─► END │
└───────────────────────────────────────────────────────────────────────────────────────────┘
   runner: timeout (60 s) · safe fallback (any failure → human queue + holding reply) · persist · traces · Slack alert
```

Specialists are **ReAct sub-graphs** (`app/agents/react.py`): `llm ⇄ tools` loop, parallel tool execution, iteration cap, a staged deterministic prefetch (`app/agents/specialists.py`), offered-tools-only, final JSON validated with a repair turn.

## Verify, then act (the core idea)

```
customer message ──► verify_transaction_issue(issue_type, subject_id)
                          │ 1. kg.plan(issue_type)     knowledge layer: which CHECKS, POLICIES, REGULATIONS, ACTIONS apply
                          │ 2. load the customer's rows (accounts, cards, transactions, transfers, disputes, alerts, waivers)
                          │ 3. run each named check with parameters from the `policies` table (tools/rules.py)
                          │ 4. decide: act | wait | deny | no_action | human     (+ approval auto|required)
                          │ 5. store a Verification row: tables, policies, graph paths and articles consulted
                          ▼
                     verification_id ──► file_dispute / reverse_fee / cancel_transfer   (each re-derives the decision itself)
```

Internal facts (risk flag, fraud-engine score, AML policy and check) influence step 4 but are removed from everything the model or customer can see.

## Agents

| agent | LLM calls | tools | notes |
|---|---|---|---|
| dispatcher | 1 (JSON, few-shot) | none | intents ∈ {payments, cards, general, escalation, off_topic}, `action_requested`, per-intent `sub_questions`; deterministic escalation triggers override it |
| payments | 1–2 | `get_transactions`, `get_transaction_detail`, `get_transfer_status`, `get_accounts`, `get_policy`, `query_knowledge_graph`, `verify_transaction_issue`, `file_dispute`, `reverse_fee`, `cancel_transfer`, `search_knowledge_base`, `create_ticket` | writes are intent-gated and verification-gated |
| cards & fraud | 1–2 | `get_cards`, `get_fraud_alerts`, `get_transactions`, `verify_transaction_issue`, `file_dispute`, `block_card`, `request_replacement_card`, `get_policy`, `query_knowledge_graph`, `search_knowledge_base` | `block_card` is protective: allowed on the customer's report |
| general | 1 | `search_knowledge_base`, `get_policy` | no account data; answers cacheable |
| escalation | 1 (internal note only) | `get_ticket_history`, `assign_to_human` | customer reply is a template |
| validator | 0–1 | none | deterministic checks, then LLM judge |

## Data model (SQLite → Postgres)

| group | tables |
|---|---|
| bank | `customers · accounts · cards · merchants · transactions (the ledger) · transfers · disputes · fraud_alerts · fee_waivers · tickets · service_components` |
| knowledge | `policies · kg_nodes · kg_edges · verifications` |
| system | `human_review_queue · audit_log · security_events · conversations · api_credentials · id_sequences · meta` |

Seed: 300 customers, ~13.5k ledger rows, ~640 transfers, 14 policies, a 96-node / 97-edge knowledge layer, and 27 scenario pools (duplicates, fraud, lost cards, stuck transfers, fees, declines, internal flags) with a ground-truth manifest `data/seed_manifest.json` that the evaluation uses.

**Knowledge graph, two layers.** The *knowledge layer* is materialised (`kg_nodes` / `kg_edges`, built at seed time from the policies table and the help articles): issue → checks, issue → policies, policy → regulation, policy → help article, issue → actions, action → gating policies. The *operational layer* is derived live from the bank tables (customer → accounts / cards → transactions → merchants / disputes; transfers), so it can never be stale and is always scoped to one customer.

## Models (measured, see `reports/00_llm_sanity.md`, `03_dispatcher*.md`)

NVIDIA's catalog lists ~80 models but only a subset is *served* on a free key. Development started on `nvidia/nemotron-3-super-120b-a12b`; **it was retired on 2026-10-03 (HTTP 410)**, so the system runs on `nvidia/nemotron-3-ultra-550b-a55b` for dispatcher, specialists, validator, merge and judge, `enable_thinking=false`, with fallbacks `meta/llama-3.2-11b-vision-instruct` → `openai/gpt-oss-20b` (never for the validator or judge). Embeddings: `nvidia/nemotron-3-embed-1b`. Safety: `nvidia/nemotron-3.5-content-safety` (advisory for confident banking requests, see case study #1). Everything is per-role configurable (`app/core/config.py`).

## Key design decisions

1. **Policy as data, decisions as code, language as LLM.** Every number is a `policies` row; the checks are unit-tested code; the LLM only routes and phrases.
2. **Verify before act.** No dispute, reversal or cancellation without a matching verification record that the tool re-derives (G-TOOL-13).
3. **The knowledge graph drives the checks.** Removing an edge removes a check (tested): the graph is behaviour, not decoration.
4. **The model never chooses identity or authority.** `customer_id` is injected from the session; writes require the customer's own words; tools are allow-listed per agent.
5. **Internal risk information is structurally unreachable.** Filtered at the tool layer, checked again by the output guard.
6. **Fail closed on quality, fail open on extras.** Validator unreachable ⇒ human review; safety model unreachable ⇒ continue.
7. **Humans are part of the graph.** `interrupt()` + checkpointer, not a side channel.
8. **A revision must remember what the first attempt did.** Completed actions are carried into the revision as evidence and as an explicit "already done" note.
9. **Observability is not optional.** Every node/LLM/tool/guardrail is a span; PII masked at the sink; the console renders them live.

## Repository map

```
app/main.py                 FastAPI factory (lifespan: DB, checkpointer, KB, graph)
app/api/                    auth · query (sync/async/SSE) · review · data (scoped views) · demo · health/metrics
app/core/                   config · security (JWT, PBKDF2) · deps (auth, rate limit) · ratelimit · metrics
app/graph/                  state · nodes · builder · runner · cache · helpers
app/agents/                 dispatcher · specialists (staged prefetch) · react · escalation · validator · prompts · rules
app/tools/                  runtime (guard pipeline) · handlers (20 tools) · verify (the investigator) · rules (checks + policy book) · kg (knowledge graph) · kb (hybrid retrieval)
app/guardrails/             input · pii · sqli · output · events
app/llm/                    gateway (NVIDIA, retries, hedging, cache) · mock · structured (JSON+repair)
app/observability/          tracing (JSONL + Langfuse)
app/integrations/slack.py   events, signature, Block Kit review, interactions
app/db/                     models · session · seed (the simulated bank) · ids
kb/                         40 markdown help-center pages (figures match the policies table)
evals/                      golden + hold-out builders · end-to-end runner · dispatcher / KB / guardrail evals · attack corpora
frontend/                   no-build console: console, data & knowledge (policies, database, graph, verifications), scope & design, review queue
loadtests/                  traffic simulator (uvicorn + mock) · locustfile
tests/                      offline suite (mock LLM) + tests/live (real LLM)
reports/                    numbered reports (see reports/INDEX.md)
docs/                       this file · bank-scope.md · bank-design.md · guardrails.md · debugging-case-studies.md · reference.md
```
