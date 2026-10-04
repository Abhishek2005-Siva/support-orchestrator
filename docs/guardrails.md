# Guardrails & methods catalog

Everything the system does to be **accurate**, **safe** and **fast**, with the file that implements it, the test that proves it, and where the measured result lives.
Every guardrail has an ID (`G-…`); the same ID appears in the code comments, trace events and reports, so you can grep for it.

> **Defence in depth.** No single layer is trusted. An attack that gets through one layer meets the next one. The diagram shows the order a customer message travels:

```
 HTTP ──► G-API   JWT, role, rate-limit, body-size, validation ─────────────────────────────┐
  │                                                                                          │
  ▼                                                                                          │
 intake ─ G-IN    sanitize · secret masking · SQLi · prompt-injection · authz probe  ──REFUSE─┤
  │                                                                                          │
  ├─► dispatcher  G-AGENT  topic control (off_topic) · escalation triggers · low-confidence  │
  ├─► safety model G-IN-08 (parallel, best-effort)                                           │
  ▼                                                                                          │
 specialists ─ G-TOOL   allow-list · identity injection · SQLi scan · schema · intent-gated writes
  │             G-LLM   schema-validated JSON · repair · uncertainty → human                 │
  ▼                                                                                          │
 validator ── G-OUT/G-VAL  deterministic grounding checks → LLM faithfulness judge ─► revise ─┤
  │                                                                                          │
  ├─ approve ──► deliver                                                                     │
  └─ human_review ──► G-HITL review queue (interrupt/resume) ◄───────────────────────────────┘
 Everything is traced (G-OBS) with PII masked; every failure degrades to the human queue (G-REL).
```

---

## 1. Input guardrails — run before any LLM sees the text (`app/guardrails/input.py`, `pii.py`, `sqli.py`)

| ID | Method | Detail | Test | Measured |
|---|---|---|---|---|
| G-IN-01 | Sanitise | Unicode NFKC, strip control + zero-width chars, collapse blank lines, enforce 2 000-char limit | `test_sanitize_*` | — |
| G-IN-02 | Secret + payment-data masking | API keys (`nvapi-`, `sk-`, `xox*`, AWS), JWTs, Bearer tokens, **full card numbers, SSNs, IBANs** in the *message* are replaced (`<SECRET>`, `<CARD-1111>`) before any model sees them; the review queue and Slack alerts re-mask as defence in depth | `test_check_input_blocks_and_masks` | eval: `adv_secret` |
| G-IN-03 | PII masking in logs/traces | emails, phones, cards (Luhn-validated), SSN, IBAN, IPs masked before anything is written to JSONL or Langfuse (also Langfuse `mask=`) | `test_phase0` PII + trace tests | PII scan of all logs of the verification run: 0 unmasked values (`05_observability_pii_check.md`) |
| G-IN-04 | Secret detection | `find_pii(kind=secret)` regexes; shared with output layer | `test_secret_detection` | |
| G-IN-05 | SQL-injection (message) | ~35 phrase-level patterns after normalisation (URL-decode ×2, comment stripping, NFKC). Strong patterns only, to keep false positives ≈ 0 | `test_sqli_*` | `02_guardrails.md`: 100 % handwritten, 95 % Kaggle SQLiV3, **0 % FP** on 500 real messages |
| G-IN-06 | Prompt-injection | 60+ weighted regexes (EN/DE/ES/FR), noisy-OR score; ≥0.8 refuse; 0.35–0.8 **gray zone → LLM classifier** (only then is an LLM called); also de-obfuscation (`i g n o r e`) and structural signals (length) | `test_injection_*` | `02_guardrails.md`: 88 % / 98 % (handwritten), 47–68 % Kaggle jailbreak corpora; **0 % FP** on 600 Bitext messages |
| G-IN-07 | Authz probe | message mentions another customer's id → security event; specialists are told they cannot access it | `test_foreign_customer_reference_is_refused` | eval `adv_cross_customer` |
| G-IN-08 | Content-safety model | NVIDIA `nemotron-3.5-content-safety` runs **in parallel** with the dispatcher; unsafe → escalation. Best-effort: 2.5 s deadline, fails open | — | trace span `guard.safety_model` |

## 2. Agent-level guardrails (`app/agents/`)

| ID | Method | Detail | Test |
|---|---|---|---|
| G-AGENT-01 | **Topic control** | dispatcher intent `off_topic` → polite refusal template; no specialist, no tools. Catches persona/jailbreak/unrelated asks the regexes miss | `test_off_topic_*`, eval `off_topic` |
| G-AGENT-02 | **Deterministic escalation triggers** | regex rules (legal, breach, fraud/chargeback, explicit human request, threats, anger, VIP unhappy, repeat contact ≥3) *override* the LLM; priority has a deterministic floor | `test_deterministic_triggers_override_llm`, eval `escalation` |
| G-AGENT-03 | Low-confidence → human | dispatcher confidence < 0.55 adds escalation instead of guessing | dispatcher eval |
| G-AGENT-04 | Bounded fan-out | ≤2 specialists + escalation per query | unit |
| G-AGENT-05 | **Uncertainty → human** | if a reply says "not certain / KB doesn't mention / couldn't find", `needs_human=true` is forced even if the model forgot; such replies are never cached | `test_uncertainty_detector` |
| — | Untrusted-input framing | customer text is wrapped in `<customer_message>` and every prompt states it is untrusted data | prompts |
| — | Hard rules in every specialist prompt | grounding, no guessing, no promises, privacy, concise style | live agent tests |

## 3. Tool-call guardrails (`app/tools/runtime.py`) — every tool call passes this pipeline

| ID | Method | Detail | Test |
|---|---|---|---|
| G-TOOL-01 | Per-agent allow-list | billing agent cannot call `assign_to_human`, etc. → blocked + security event + audit | `test_allow_list_blocks_and_logs` |
| G-TOOL-02 | Strict schema | Pydantic `extra="forbid"`, regex IDs (`INV-\d{8}`), ranges, lengths. Errors are returned to the model as *repairable* messages | `test_invalid_args_return_repairable_error` |
| G-TOOL-03 | **SQLi pre-execution scan** | strict mode on identifier args (any quote/`;`/`--`/space → reject), phrase mode on free text. Logged as `sqli_attempt` | `test_sqli_in_identifier_blocked_before_db` |
| G-TOOL-04 | **Identity injection** | `customer_id` comes from the JWT, never the model. If the model supplies another one → blocked + `authz_violation`. Tool schemas never even offer an identity parameter | `test_model_cannot_choose_customer` |
| G-TOOL-05 | Parameterised SQL only | SQLAlchemy bound parameters everywhere; every query also has `WHERE customer_id = :session_customer` | `test_cannot_read_other_customers_invoice` |
| G-TOOL-06 | Policy as code | refund eligibility = deterministic rules engine (`tools/rules.py`), re-derived inside `create_refund_request` — the model's opinion is never trusted | `test_refund_*` (ground-truth manifest) |
| G-TOOL-07 | Audit log | every write and every blocked call → `audit_log` (+ `security_events`, `logs/security_events.jsonl`) | `test_audit_log_written…` |
| G-TOOL-08 | Budget + timeout | ≤12 calls/query, ≤4 per tool, 10 s per call, 6 000-char result cap | `test_tool_call_budget` |
| G-TOOL-09 | Idempotent writes | repeating `create_refund_request` / `create_ticket` / `assign_to_human` never duplicates | `test_refund_small_auto_approved_idempotent…` |
| G-TOOL-10 | **Intent-gated writes** | `create_refund_request` requires refund intent in the *customer's* message **and** the dispatcher's semantic `refund_requested` verdict (so a policy *question* containing the word "refund" cannot file one); `create_ticket` requires a ticket/follow-up request. Stops "model decided on its own" actions |
| G-TOOL-11 | **Refund filing is a policy path, not a model whim** | when the dispatcher flags an explicit refund *request*, the billing prefetch runs the eligibility engine and, if eligible, files the request through the same guarded tool (auto-approve ≤ $100, else pending human approval); the model only explains the outcome | `test_refund_write_is_intent_gated…`, `test_ticket_write_is_intent_gated` |
| G-TOOL-12 | Only offered tools run | tools hidden this turn (e.g. KB re-search after a strong hit) are rejected even if the model "continues the pattern" | live agent tests |
| — | No enumeration oracle | a foreign invoice id and a non-existent id return the *same* error | `test_cannot_read_other_customers_invoice` |

Data layer: **G-DATA-01** atomic id allocation (`id_sequences` + `UPDATE … RETURNING`) — found by the load/eval harness (see `docs/debugging-case-studies.md` #3); regression test `test_concurrent_writes_get_unique_ids_no_failures`.

## 4. LLM-layer controls (`app/llm/`)

| ID | Method | Purpose |
|---|---|---|
| G-LLM-01 | JSON mode + Pydantic validation + one repair turn | open models are less reliable at strict JSON; plain-prose final answers are accepted as the reply (validator judges them) |
| R-01/02 | Global semaphore + token bucket | stay under the free-tier limit (measured: 429s above ≈100 rpm / concurrency 4) |
| R-03 | Retry with exponential backoff + jitter; honours `Retry-After` | 429 / 5xx / timeouts |
| R-04 | Fallback model chain per role | primary down / 404 / 410 → next model |
| R-08 | **Dead-model circuit breaker** | 404/410/403 ⇒ the model is skipped for 15 min (no wasted round trip per call); `llm.model_unavailable` trace event. Added after the primary model was retired mid-project (case study #15) |
| R-05 | Exact-match LRU cache (temperature 0) | repeated calls cost nothing |
| R-06 | Per-call timeout (20 s) | nothing can hang |
| R-07 | **Hedged requests** | the free endpoint stalls ~3 % of calls for 20–30 s; once a request has actually been *sent* and 2.5–5 s (per role) pass without an answer, an identical request is raced. The timer excludes time spent queueing behind the rate limiter (including it hedged 31 % of calls and doubled the load — case study #11) |
| L-01 | `enable_thinking=false` per role | ≈3× faster generation; thinking can be turned on per role in `config.py` |
| O-01 | Every call is a traced span | model, tokens, latency, retries, fallback |

## 5. Output guardrails — the validator (`app/guardrails/output.py`, `app/agents/validator.py`)

Layer 1 is **deterministic** (no LLM, ~1 ms) and re-derives everything from the evidence (tool results + KB text), never from what the model claims:

| ID | Check | Severity |
|---|---|---|
| G-OUT-01 | PII / secrets / full card numbers / SSN / IBAN in the reply (own email allowed) | critical |
| G-OUT-02 | Forbidden promises ("I guarantee", "definitely get a refund"); liability admissions | critical / warning |
| G-OUT-03 | **Action claims vs evidence**: "refund approved/issued" without an *approved* refund; "request filed" without any request; "approved" while evidence says *pending approval* | critical |
| G-OUT-04 | **Numeric grounding**: every `$` amount (critical), document id `INV-/REF-/TCK-/HRQ-/PAY-` (critical), date, and number-with-unit ("14 days", "600 requests/min") must exist in the evidence or the customer's own message | critical / warning |
| G-OUT-05 | Account facts need account evidence; `sources` are derived from real tool calls, not trusted from the model | critical |
| G-OUT-06 | Hygiene: tool names, prompt/rule text, JSON/code fences, role-play compliance, profanity, empty/over-long | critical |
| G-OUT-07 | Other customers' ids must never appear | critical |
| G-OUT-08 | **Unsupported negative claims**: "Orbit does not offer / include X" needs X's words in the evidence (absence of evidence ≠ evidence of absence); first-person limits ("I couldn't find…") and hedged phrasing ("not mentioned in the knowledge base") are allowed | critical |
| G-OUT-09 | **Unfulfilled action promises**: "I'll submit the refund now" without a write action in the evidence | critical |

Layer 2 is an **LLM faithfulness judge** (temperature 0, JSON schema, claim-by-claim vs evidence; calibrated against paraphrases; sees *every* evidence item). Decision logic **G-VAL-01**:

```
specialist says it cannot answer ............ human_review
critical deterministic issue ................ revise (feedback to specialist) ─► human_review after max_revisions (1)
judge unreachable ........................... human_review   (fail CLOSED — never send an unverified answer)
unsupported claim / policy violation ........ revise ─► human_review
deterministic warnings (+ judge < 0.9) ...... revise ─► human_review
confidence (0.75·judge + 0.25·specialist − 0.05·warnings) < 0.7 ... human_review
otherwise ................................... approve
```
The validator and the judge **never fall back to a weaker model** (a weak judge rejected 68 % of good drafts vs 8 % on the primary — case study #16): more retries on the primary, then fail closed to a human. Every decision (reply, evidence sources, issues, judge output) is appended to `logs/validator_audit.jsonl` for manual review. Tests: `tests/test_output_guardrails.py` (14, built from real tool evidence), `tests/live/test_validator.py` (8, live LLM).

## 6. Human-in-the-loop (`app/graph/`, `app/api/review.py`, `app/integrations/slack.py`)

| ID | Method |
|---|---|
| G-HITL-01 | Validator `human_review` ⇒ review row + holding reply to the customer + LangGraph `interrupt()`: the run is **checkpointed and paused** until staff approve / edit / reject (`POST /v1/review-queue/{id}/resolve` or Slack buttons), then the graph **resumes**. Refunds over $100 are filed as pending approval *without* blocking the (truthful) reply |
| — | Escalation replies are **templates** (no hallucination, no admissions, no promises beyond the tier ETA from policy) |
| — | Priority floor is deterministic: legal/security ⇒ critical; angry/fraud/VIP/repeat ⇒ ≥ high |

## 7. API & platform (`app/main.py`, `app/api/`, `app/core/`)

| ID | Method | Test |
|---|---|---|
| G-API-01 | JWT (HS256, 30-min expiry, roles `customer` / `agent_staff` / `admin`); `none`-alg and forged tokens rejected | `test_missing_invalid_expired_tampered_tokens`, `test_role_enforcement` |
| G-API-02 | PBKDF2-SHA256 secret hashing, constant-time compare; unknown id and wrong secret are indistinguishable (same response, same work) | `test_token_ok_and_bad_credentials_same_shape` |
| G-API-03 | Sliding-window rate limit 100 req/min/user → 429 + `Retry-After`; per-IP login limit | `test_rate_limit_429_with_retry_after` |
| G-API-04 | Login lockout after 5 failures (60 s) | `test_login_lockout…` |
| G-API-05 | Body-size cap (413), Pydantic `extra="forbid"` (a body that tries to set `customer_id` is a 422), generic 500s with no internals | `test_oversized_body_413`, `test_unhandled_error_returns_generic_500` |
| — | Bounded in-flight queries (503 + `Retry-After` when saturated); `Cache-Control: no-store`, `X-Content-Type-Options` | load test |
| G-SLACK-01 | Slack request signature (HMAC-SHA256, 5-min replay window, constant-time) | `test_slack_signature_challenge_and_replay` |
| G-SLACK-02 | Event de-duplication + ignore Slack retries (ack immediately, process async) | `test_slack_message_from_linked_user…` |
| G-SLACK-03 | Only Slack users linked to an Orbit account can use the bot; only linked **staff** can press review buttons | `test_slack_review_buttons_staff_only…` |
| G-SLACK-04 | Customer text shown in the escalation channel is PII-masked | |

## 8. Observability (`app/observability/tracing.py`)

* One **trace per query** (`trace_id = query_id`), one span per node / LLM call / tool / guardrail / retriever; local JSONL (`logs/traces/`) always, mirrored to **Langfuse** when keys are set (v4 SDK, `mask=` hook, scores: validator confidence, human-review flag, latency).
* **G-OBS-01** security events → table `security_events` + `logs/security_events.jsonl` (+ trace event).
* Validator audit log, Slack outbox (`logs/slack_outbox.jsonl` when no bot token), `scripts/trace_view.py` span-tree viewer, `scripts/report.py` aggregates (p50/p95 per node, tool error rate, human-review rate, token usage, injection attempts).

## 9. Reliability

| ID | Method |
|---|---|
| G-REL-01 | Any unhandled failure / timeout ⇒ safe fallback: apologetic holding reply + human-review row. Chaos-tested with 20 % injected LLM failures: 0 × 5xx (`reports/06_load_test.md`) |
| — | Per-node containment: a failing specialist becomes a "needs human" output instead of crashing the request; escalation never fails (template fallback); safety model fails open (it is an extra layer) while the validator fails **closed** |
| — | Hard caps everywhere: ReAct iterations (4), tool calls (12/4), revisions (1), request timeout (60 s), LangGraph recursion limit |

## 10. Accuracy methods (not guardrails, but why answers are right)

| ID | Method | Evidence |
|---|---|---|
| A-KB-01 | Dense-led hybrid retrieval (`nvidia/nemotron-3-embed-1b` + 0.05·BM25). Chosen by experiment: BM25 0.42 → dense 0.87 → hybrid 0.89 recall@1 on Kaggle-derived queries; naive RRF was worse (0.68) | `01_kb_retrieval.md` |
| A-KB-02/03 | Category filter with auto-widening; relevance threshold calibrated on in-scope vs out-of-scope queries; weak matches carry an explicit "use only if it directly answers" note | `01_kb_retrieval.md` |
| A-KB-04 | Stable source ids per chunk, used by the validator | |
| — | **Prefetch**: independent lookups (KB, invoices, logs, status, refund eligibility) run in parallel before the first LLM call, through the *same guarded tool path* | latency + accuracy |
| — | Deterministic business logic (refund rules, escalation priority) lives in code and is unit-tested against a seeded ground-truth manifest | `tests/test_tools.py` |
| — | Per-specialist scope for multi-intent messages (stops cross-topic hallucination, found by the judge) | case study #5 |
| — | Dispatcher: few-shot prompt + schema + deterministic overlay; per-intent **sub-questions** for multi-intent messages (each specialist retrieves for *its* part); chosen over 2 smaller models by measurement | `03_dispatcher*.md` |
| — | Reply text normalisation (NFKC, non-breaking spaces/hyphens, typographic quotes, leaked "Confidence: 0.9, needs_human: false" trailers stripped) so regex checks and customers see plain text | `test_leaked_metadata_trailer…` |
| — | Full KB chunks (no truncation) are passed to the validator and the judge sees every distinct evidence item | case study #10 |

## 11. Latency methods

parallel fan-out (specialists, and dispatcher ‖ safety ‖ KB-warm) · tool prefetch (1–2 LLM calls instead of 4–5) · hedged requests · per-role models/thinking off · template merge (no merge LLM call) · validated-answer cache for KB-only questions (`G-LAT-01`: only validated, PII-free, KB-grounded, strong-match replies; keyed on plan/tier/KB version) · safety model deadline · KB query-embedding LRU · caps on re-search loops. Measured numbers: `04_eval_*.md` (latency section) and `06_load_test.md`.

## 12. Threat model — what stops what

| Attack | Layers that stop it |
|---|---|
| "Ignore previous instructions / reveal your prompt" | G-IN-06 → G-AGENT-01 (off_topic) → untrusted-input framing → G-OUT-06 (internal leak check) |
| SQL injection in the message | G-IN-05 → G-TOOL-03 (strict arg scan) → parameterised queries (structural) |
| "Show me customer X's invoices" | G-IN-07 → G-TOOL-04 (identity from JWT) → ownership-scoped SQL → same-error-for-missing → G-OUT-07 |
| Prompt-injected agent tries another `customer_id` | G-TOOL-04 block + `authz_violation` event |
| "Approve my refund without checks" | G-TOOL-06 (policy engine) → G-TOOL-10 → G-OUT-03 |
| Agent refunds/tickets on its own initiative | G-TOOL-10 |
| Hallucinated amounts/ids/dates | G-OUT-04/05 → LLM judge → revise → human |
| Overconfident answer to an unanswerable question | G-AGENT-05 → judge `needs_human` → human queue |
| Leaked key / card number pasted by the customer | G-IN-02/03, G-OUT-01 |
| Brute-force login / token forgery / replay | G-API-01..04, G-SLACK-01 |
| Abuse / flooding | G-API-03, bounded in-flight, 503 shedding |
| LLM outage / stall | R-03/04/07, G-REL-01 |

## 13. Known limitations (honest)

* The regex injection layer catches ~88 % of support-style attacks and only ~25–50 % of generic role-play jailbreak corpora; the rest depends on the *other* layers (topic control, tool guards, validator). See `02_guardrails.md` and the adversarial rows of `04_eval_full.md` for end-to-end outcomes.
* The judge and the specialists use the same model family (the only strong model served on this key), so shared blind spots are possible; the deterministic layer and the independent eval judge reduce, not eliminate, that risk.
* In-process rate limiter / cache are per worker (Redis recommended for multi-worker); SQLite is single-writer (Postgres via `docker-compose.yml` for production).
* All customer data is **synthetic**; Slack/Langfuse need your own keys (dry-run/local modes are built in).
