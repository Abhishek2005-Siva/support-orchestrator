# Reports index — every measured number, and how much to trust it

Models: development started on `nemotron-3-super-120b`; **it was retired on 2026-10-03 (HTTP 410)**, so every *final* number below was produced with `nvidia/nemotron-3-ultra-550b-a55b` (fallbacks: llama-3.2-11b, gpt-oss-20b; the validator/judge never fall back). Data are synthetic. Free-tier endpoint: results vary run to run by a few points.

## Headline results

| claim in the blueprint / résumé | what was measured | where | trust |
|---|---|---|---|
| "98 % accuracy" | **98.1 %** pass on the 260-case golden set (answerable questions 96.5 %, routing 99.2 %, refund decisions 100 %, escalation recall 100 %) — **but this set was used while tuning**, so it is optimistic. On the **hold-out set** (70 fresh cases, never tuned against) the *first, unbiased* pass was **90.0 %** (answerable 88.4 %, routing 97.7 %, escalation recall 100 %, guardrails 100 %, refund-request recall 72 % ← a real gap, then fixed → 94.3 % post-fix, no longer independent). **Quote ≈ 90 % as the unbiased figure.** | `04_eval_full.md`, `04_eval_holdout_FIRSTPASS_unbiased.md`, `04_eval_holdout.md` | golden: optimistic; hold-out first pass: best estimate |
| hallucination / grounding | independent LLM faithfulness judge: **7.1 %** of 112 replies flagged on the golden set (0 % of 42 on the first hold-out pass). Judge noise is non-zero (it flagged some supported claims early on; see case studies #10, #16); manual review list is in the report. Deterministic checks found 0 leaked internal text / other-customer data. | `04_eval_full.md` | medium |
| "<2 s latency" | **NOT met with the free API.** Single-user, live model, answer cache off: **p50 7.8 s, p95 20.4 s** (dispatcher ≈2.0 s, specialist ≈3.4 s, validator ≈1.9 s; ≈4.4 LLM calls/query). Hard-rejected inputs: ~10 ms; off-topic: <1 s. Validated KB-only answers served from the answer cache: milliseconds. | `04_eval_latency.md` | good |
| "100+ concurrent users" | orchestration layer, **mock LLM**, 1 uvicorn worker: 100 users with realistic think time (5–15 s) → **p50 0.9 s / p95 3.6 s, 0 errors**; 100 users with *no* think time saturate one worker at ≈28 req/s (p50 3.6 s); 250 users: 0 5xx. Chaos (20 % of LLM calls failing): **0 × 5xx**, every customer answered (degrades to human queue). With the real free endpoint capacity is ≈15–25 queries/min per key. | `06_load_test.md` | good for the orchestration layer only |
| "500+ queries/day" | 500 queries completed in 51 s (mock LLM, 20 users); real-LLM capacity ≈ 20–35 k/day per key at the measured rate limit | `06_load_test.md`, `00_llm_sanity.md` | good |
| "4 h → 15 min debugging" | **not measured.** 17 real incidents with trace evidence are written up; a worksheet to time yourself is included | `../docs/debugging-case-studies.md` | n/a |

## All reports

| file | what it is |
|---|---|
| `00_llm_sanity.md` | API key check, per-model probe, rate-limit burst test (run on the original model) |
| `01_kb_retrieval.md` | retrieval: BM25 vs dense vs RRF vs dense-led hybrid; BM25-weight sweep; "no article" threshold calibration |
| `02_guardrails.md` | SQLi + prompt-injection detectors on handwritten and Kaggle corpora, with and without the LLM gray-zone check |
| `03_dispatcher*.md` | dispatcher routing accuracy on 186 labelled cases; model comparison (done on the retired model) |
| `04_eval_full.md` | **golden-set end-to-end evaluation (final)** + failures list + judge-flagged replies |
| `04_eval_holdout_FIRSTPASS_unbiased.md` / `04_eval_holdout.md` | hold-out set: first pass (unbiased) / after fixing what it exposed |
| `04_eval_latency.md` | single-user latency, per stage |
| `04_eval_pii_check.md`, `05_observability_pii_check.md` | verification of the card-number masking fix (0 unmasked values) |
| `04_eval_*_pass*.md`, `04_eval_quick/answerable/mock.md`, `eval_runs/*` | **audit trail of earlier iterations** (kept on purpose: they show the failures that led to each fix). `04_eval_full_superEOL_firstpass.md` / `eval_runs/full_superdegraded` is the run corrupted by the model retirement |
| `05_observability.md` | what Langfuse dashboards would show (latency per node, LLM usage, tool error rates, security events, validator decisions, PII scan) generated from the final eval's traces |
| `06_load_test.md` | load / chaos / rate-limit tests |
| `PROGRESS.md` | chronological build log |
| `eval_runs/<run>/results.jsonl` | every case: message, reply, status, intents, validator verdict, failure reasons — read these to judge quality yourself |
| `eval_runs/<run>/logs/` | that run's traces, `validator_audit.jsonl`, `security_events.jsonl`, `slack_outbox.jsonl` |

## Caveats worth knowing

* Labels for Kaggle-derived routing sets are noisy (documented in `evals/label_maps.py`); a few "failures" in the reports are label issues, flagged as such in the case studies.
* The independent judge and the validator are the same model family; deterministic checks and DB-state checks are what make the refund/escalation numbers solid.
* Concurrency-based latency numbers in the evaluation runs (`04_eval_full.md`) measure queueing behind the 70-requests/min client-side rate limit (Little's law), not single-query latency — use `04_eval_latency.md`.
* The headline golden run predates the card-number masking fix (it only affects replies to messages containing card numbers; verified separately in `04_eval_pii_check.md`).
