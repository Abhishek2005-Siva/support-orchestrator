# 05 — Observability report
_Source: `reports/eval_runs/full/logs/traces` · 4247 spans · 260 traces_

## Queries

- 260 queries; latency p50 17427 ms, p95 42714 ms, max 65764 ms
- outcomes: {'delivered': 153, 'human_review': 64, 'rejected': 43}
- **human-review rate**: 24.6% · rejected at input: 16.5%
- validator confidence: mean 0.87, p10 0.50

## Latency per node / span (ms)

| kind | span | n | p50 | p95 | max |
|---|---|---|---|---|---|
| trace | support_query | 260 | 17427 | 42714 | 65764 |
| agent | agent.specialist | 204 | 6845 | 24480 | 33652 |
| agent | agent.dispatcher | 217 | 5920 | 18643 | 37939 |
| llm | llm.dispatcher | 217 | 5918 | 18642 | 37938 |
| embedding | embed | 352 | 2980 | 8636 | 18008 |
| agent | agent.validator | 213 | 3378 | 13731 | 28866 |
| llm | llm.validator | 159 | 4330 | 18084 | 28859 |
| llm | llm.billing | 116 | 6100 | 17073 | 30249 |
| llm | llm.technical | 76 | 6592 | 20820 | 27425 |
| llm | llm.eval | 112 | 4336 | 9788 | 11405 |
| agent | agent.escalation | 64 | 6323 | 17201 | 24161 |
| llm | llm.escalation | 64 | 6308 | 17184 | 24145 |
| retriever | kb.search | 317 | 1 | 4805 | 9940 |
| guardrail | guard.safety_model | 217 | 1116 | 2502 | 2506 |
| llm | llm.safety | 217 | 1116 | 2502 | 2505 |
| llm | llm.general | 36 | 6020 | 28861 | 33642 |
| tool | tool.search_knowledge_base | 205 | 2 | 3176 | 9754 |
| span | node.intake | 260 | 5 | 18 | 9472 |
| guardrail | guard.input | 260 | 0 | 3 | 9470 |
| llm | guard.injection_llm | 6 | 2603 | 9467 | 9467 |
| tool | tool.get_service_status | 61 | 7 | 17 | 666 |
| tool | tool.get_invoices | 108 | 9 | 17 | 22 |
| tool | tool.assign_to_human | 64 | 14 | 24 | 28 |
| tool | tool.check_refund_eligibility | 67 | 11 | 22 | 34 |
| tool | tool.create_refund_request | 21 | 21 | 26 | 29 |
| tool | tool.get_user_logs | 61 | 6 | 13 | 19 |
| tool | tool.get_ticket_history | 64 | 4 | 12 | 15 |
| tool | tool.run_diagnostic | 13 | 6 | 28 | 28 |
| tool | tool.get_payment_status | 3 | 5 | 25 | 25 |
| span | node.merge | 213 | 0 | 0 | 0 |

## LLM usage

- 1003 generations · 1,617,267 prompt + 52,438 completion tokens · per query ≈ 3.9 calls, 6,422 tokens
- models: {'nvidia/nemotron-3.5-content-safety': 159, 'nvidia/nemotron-3-ultra-550b-a55b': 786, None: 58}
- retried: 98 · fallback model used: 0 · cache hits: 0

## Tool calls

| tool | calls | errors (bad args / not found) | blocked by guardrail |
|---|---|---|---|
| tool.search_knowledge_base | 205 | 0 (0%) | 0 |
| tool.get_invoices | 108 | 0 (0%) | 0 |
| tool.check_refund_eligibility | 67 | 0 (0%) | 0 |
| tool.get_ticket_history | 64 | 0 (0%) | 0 |
| tool.assign_to_human | 64 | 0 (0%) | 0 |
| tool.get_user_logs | 61 | 0 (0%) | 0 |
| tool.get_service_status | 61 | 0 (0%) | 0 |
| tool.create_refund_request | 21 | 0 (0%) | 0 |
| tool.run_diagnostic | 13 | 0 (0%) | 0 |
| tool.get_payment_status | 3 | 1 (33%) | 0 |

## Security events (`logs/security_events.jsonl`)

{'unsafe_content': 18, 'prompt_injection': 33, 'sql_injection': 10, 'authz_probe': 3}

## Notable events

{'llm.final_parse_failed': 39, 'security.unsafe_content': 18, 'graph.revise': 12, 'security.input_flagged': 46, 'security.prompt_injection': 33, 'security.sql_injection': 10, 'security.authz_probe': 3}

## Validator decisions (`logs/validator_audit.jsonl`)

- verdicts: {'approve': 170, 'revise': 12, 'human_review': 31}; layers: {'both': 159, 'deterministic': 28, 'specialist': 26}
- top issue codes: [('specialist_needs_human', 26), ('unsupported_claim', 10), ('ungrounded_date', 8), ('unsupported_negative_claim', 5), ('ungrounded_number', 2), ('pii_leak', 2), ('does_not_answer', 2)]

## PII scan of logs

- scanned 4 JSONL files: **3 unmasked sensitive values** {'email': 1, 'card': 2} (target 0; `@example.com` synthetic addresses excluded from this count — they appear only where the customer's own email is part of ground-truth data)