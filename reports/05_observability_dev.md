# 05 — Observability report
_Source: `logs/traces` · 7879 spans · 37 traces_

## Queries

- 37 queries; latency p50 11530 ms, p95 29638 ms, max 30568 ms
- outcomes: {'delivered': 24, 'human_review': 9, 'rejected': 4}
- **human-review rate**: 24.3% · rejected at input: 10.8%
- validator confidence: mean 0.86, p10 0.80

## Latency per node / span (ms)

| kind | span | n | p50 | p95 | max |
|---|---|---|---|---|---|
| llm | llm.dispatcher | 1668 | 5026 | 37546 | 93142 |
| agent | agent.dispatcher | 1597 | 5059 | 39893 | 128295 |
| agent | agent.specialist | 67 | 7582 | 23724 | 33854 |
| trace | support_query | 37 | 11530 | 29638 | 30568 |
| embedding | embed | 497 | 819 | 1257 | 10694 |
| retriever | kb.search | 3093 | 0 | 877 | 10735 |
| llm | llm.technical | 120 | 1832 | 8751 | 27519 |
| llm | llm.billing | 60 | 1729 | 7116 | 8980 |
| llm | llm.validator | 38 | 1264 | 5647 | 6221 |
| agent | agent.validator | 39 | 1232 | 5652 | 6227 |
| guardrail | guard.safety_model | 32 | 1560 | 4322 | 4815 |
| llm | llm.safety | 32 | 1559 | 4322 | 4815 |
| llm | llm.general | 17 | 1736 | 7193 | 7193 |
| tool | tool.search_knowledge_base | 107 | 192 | 1153 | 1720 |
| agent | agent.escalation | 9 | 1473 | 2396 | 2396 |
| llm | llm.escalation | 9 | 1422 | 2344 | 2344 |
| span | node.merge | 38 | 0 | 1345 | 8224 |
| llm | llm.merge | 2 | 8223 | 8223 | 8223 |
| span | node.intake | 37 | 15 | 33 | 44 |
| tool | tool.get_invoices | 45 | 11 | 40 | 57 |
| tool | tool.check_refund_eligibility | 26 | 22 | 35 | 40 |
| tool | tool.get_user_logs | 34 | 9 | 54 | 58 |
| tool | tool.get_service_status | 33 | 10 | 43 | 48 |
| tool | tool.create_refund_request | 14 | 21 | 40 | 40 |
| tool | tool.assign_to_human | 10 | 25 | 66 | 66 |
| tool | tool.run_diagnostic | 26 | 8 | 11 | 11 |
| tool | tool.create_ticket | 7 | 24 | 31 | 31 |
| tool | tool.get_ticket_history | 10 | 9 | 24 | 24 |
| tool | tool.get_customer_profile | 48 | 0 | 8 | 9 |
| guardrail | guard.input | 94 | 0 | 1 | 1 |

## LLM usage

- 1946 generations · 2,627,162 prompt + 103,595 completion tokens · per query ≈ 52.6 calls, 73,804 tokens
- models: {'nvidia/nemotron-3-super-120b-a12b': 746, 'nvidia/nemotron-3.5-lightning-30b-a3b': 451, 'nvidia/nemotron-3-nano-omni-30b-a3b-reasoning': 1, None: 26, 'meta/muse-glimmer-30b': 113, 'meta/llama-3.2-11b-vision-instruct': 572, 'openai/gpt-oss-20b': 2, 'mock': 3, 'nvidia/nemotron-3.5-content-safety': 32}
- retried: 220 · fallback model used: 159 · cache hits: 1

## Tool calls

| tool | calls | errors (bad args / not found) | blocked by guardrail |
|---|---|---|---|
| tool.search_knowledge_base | 107 | 0 (0%) | 1 |
| tool.get_customer_profile | 48 | 0 (0%) | 40 |
| tool.get_invoices | 45 | 15 (33%) | 0 |
| tool.get_user_logs | 34 | 0 (0%) | 0 |
| tool.get_service_status | 33 | 0 (0%) | 0 |
| tool.run_diagnostic | 26 | 0 (0%) | 0 |
| tool.check_refund_eligibility | 26 | 0 (0%) | 0 |
| tool.get_payment_status | 18 | 18 (100%) | 0 |
| tool.nope | 15 | 15 (100%) | 0 |
| tool.create_refund_request | 14 | 0 (0%) | 2 |
| tool.get_ticket_history | 10 | 1 (10%) | 0 |
| tool.assign_to_human | 10 | 0 (0%) | 0 |
| tool.create_ticket | 7 | 0 (0%) | 0 |

## Security events (`logs/security_events.jsonl`)

{'prompt_injection': 4, 'unrequested_write_blocked': 2}

## Notable events

{'llm.structured_repair': 77, 'security.input_flagged': 31, 'llm.final_parse_failed': 41, 'dispatcher.fallback': 35, 'graph.revise': 9, 'security.prompt_injection': 4, 'security.unrequested_write_blocked': 2}

## Validator decisions (`logs/validator_audit.jsonl`)

- verdicts: {'approve': 19, 'revise': 3, 'human_review': 2}; layers: {'both': 23, 'specialist': 1}
- top issue codes: [('unsupported_claim', 9), ('ungrounded_date', 4), ('ungrounded_number', 3), ('specialist_needs_human', 1)]

## PII scan of logs

- scanned 4 JSONL files: **0 unmasked sensitive values**  (target 0; `@example.com` synthetic addresses excluded from this count — they appear only where the customer's own email is part of ground-truth data)