# 04 — End-to-end evaluation: `pii_check`
_15 golden cases · wall time 81s · concurrency 2 · LLM mode `live` · models: dispatcher/specialist/validator = `nvidia/nemotron-3-ultra-550b-a55b`_

Checks are deterministic (regex facts, DB side effects, review rows, leak patterns) — see `evals/run_eval.py`. Ground truth: `data/seed_manifest.json`, `kb/*.md`, Kaggle corpora.

## Headline

**Overall pass rate: 100.0%** (15/15)

| category | n | pass | |
|---|---|---|---|
| adv_cross_customer | 8 | 100.0% |  |
| adv_secret | 3 | 100.0% |  |
| escalation_repeat | 4 | 100.0% |  |

## Accuracy

- **Answerable-question accuracy** (billing / technical / FAQ / multi-intent; every required fact present, no forbidden claims, correct DB side-effects): **n/a** (0 cases)
- **Routing accuracy** (dispatcher intents vs label): **n/a** (0 labelled cases)
- **Escalation recall** (should-escalate cases routed to a human): **100.0%** (4); false escalations on answerable cases: **n/a** (0/0)
- **Unnecessary human-review rate** on answerable questions: **n/a** (0/0)
- **Unanswerable questions handled without hallucination** (abstain or human): **n/a** (0)
- **Refund decisions correct (DB state)**: **n/a** (0)

## Guardrails

- **Cross-customer data requests**: safe outcome in **100.0%** of 8 (hard-rejected at input: 3; handled safely downstream: 5)
- **Secrets / card numbers pasted by user**: safe outcome in **100.0%** of 3 (hard-rejected at input: 0; handled safely downstream: 3)
- **False-positive rate on benign look-alike messages**: **n/a** rejected of 0
- **Data/prompt leaks observed in replies**: **0** (target 0)
- Validator: 1 cases needed a revision loop; 10 ended in human review in total

## Latency (end-to-end, answer cache OFF)

| metric | p50 | p95 | max |
|---|---|---|---|
| all queries (ms) | 9555 | 22651 | 22651 |
| input guard (ms, n=15) | 1 | 1 | 1 |
| dispatcher (ms, n=12) | 4013 | 10669 | 10669 |
| safety model (parallel) (ms, n=12) | 1064 | 2506 | 2506 |
| specialist (parallel max) (ms, n=5) | 9254 | 14471 | 14471 |
| escalation (ms, n=10) | 5494 | 10922 | 10922 |
| validator (ms, n=11) | 3 | 2983 | 2983 |
| merge (ms, n=11) | 0 | 0 | 0 |

| category | p50 ms | p95 ms |
|---|---|---|
| adv_cross_customer | 5240 | 15538 |
| adv_secret | 20129 | 22651 |
| escalation_repeat | 9581 | 13612 |

- LLM calls per query: mean 4.0, max 8; tokens per query: mean 5722
- Gateway stats: `{'calls': 75, 'cache_hits': 0, 'retries': 10, 'rate_limited': 5, 'fallbacks': 0, 'errors': 0, 'hedges': 17, 'prompt_tokens': 65780, 'completion_tokens': 2884}`
- Note: latency is dominated by the free NVIDIA endpoint (p50 ≈ 1 s/call, p90 ≈ 4 s, ~3 % stalls > 20 s). Orchestration overhead alone is measured in `reports/06_load_test.md` (mock LLM).

## Failures (0) — for manual review
