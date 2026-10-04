# 05 — Observability report
_Source: `reports/eval_runs/pii_check/logs/traces` · 213 spans · 15 traces_

## Queries

- 15 queries; latency p50 9566 ms, p95 22663 ms, max 22663 ms
- outcomes: {'human_review': 10, 'rejected': 3, 'delivered': 2}
- **human-review rate**: 66.7% · rejected at input: 20.0%
- validator confidence: mean 0.90, p10 0.89

## Latency per node / span (ms)

| kind | span | n | p50 | p95 | max |
|---|---|---|---|---|---|
| trace | support_query | 15 | 9566 | 22663 | 22663 |
| agent | agent.escalation | 10 | 5494 | 10922 | 10922 |
| llm | llm.escalation | 10 | 5467 | 10887 | 10887 |
| agent | agent.specialist | 6 | 9254 | 14471 | 14471 |
| agent | agent.dispatcher | 12 | 4013 | 10669 | 10669 |
| llm | llm.dispatcher | 12 | 4011 | 10667 | 10667 |
| llm | llm.billing | 6 | 5053 | 8534 | 8534 |
| llm | llm.technical | 3 | 7425 | 8051 | 8051 |
| guardrail | guard.safety_model | 12 | 1064 | 2506 | 2506 |
| llm | llm.safety | 12 | 1064 | 2505 | 2505 |
| agent | agent.validator | 12 | 6 | 2983 | 2983 |
| llm | llm.validator | 5 | 1448 | 2977 | 2977 |
| embedding | embed | 12 | 519 | 2077 | 2077 |
| span | node.intake | 15 | 12 | 159 | 159 |
| tool | tool.assign_to_human | 10 | 19 | 22 | 22 |
| tool | tool.get_ticket_history | 10 | 14 | 26 | 26 |
| tool | tool.get_invoices | 4 | 10 | 30 | 30 |
| tool | tool.get_service_status | 2 | 21 | 21 | 21 |
| tool | tool.get_user_logs | 2 | 18 | 18 | 18 |
| tool | tool.get_payment_status | 2 | 13 | 13 | 13 |
| tool | tool.search_knowledge_base | 6 | 2 | 11 | 11 |
| tool | tool.check_refund_eligibility | 1 | 17 | 17 | 17 |
| retriever | kb.search | 6 | 1 | 9 | 9 |
| guardrail | guard.input | 15 | 1 | 1 | 1 |
| tool | tool.run_diagnostic | 1 | 8 | 8 | 8 |
| span | node.merge | 12 | 0 | 0 | 0 |

## LLM usage

- 48 generations · 65,780 prompt + 2,884 completion tokens · per query ≈ 3.2 calls, 4,578 tokens
- models: {'nvidia/nemotron-3.5-content-safety': 8, None: 4, 'nvidia/nemotron-3-ultra-550b-a55b': 36}
- retried: 8 · fallback model used: 0 · cache hits: 0

## Tool calls

| tool | calls | errors (bad args / not found) | blocked by guardrail |
|---|---|---|---|
| tool.get_ticket_history | 10 | 0 (0%) | 0 |
| tool.assign_to_human | 10 | 0 (0%) | 0 |
| tool.search_knowledge_base | 6 | 0 (0%) | 0 |
| tool.get_invoices | 4 | 0 (0%) | 0 |
| tool.get_payment_status | 2 | 1 (50%) | 0 |
| tool.get_user_logs | 2 | 0 (0%) | 0 |
| tool.get_service_status | 2 | 0 (0%) | 0 |
| tool.check_refund_eligibility | 1 | 0 (0%) | 0 |
| tool.run_diagnostic | 1 | 0 (0%) | 0 |

## Security events (`logs/security_events.jsonl`)

{'prompt_injection': 3, 'authz_probe': 3, 'unsafe_content': 6}

## Notable events

{'security.input_flagged': 6, 'security.prompt_injection': 3, 'security.authz_probe': 3, 'security.unsafe_content': 6, 'graph.revise': 1}

## Validator decisions (`logs/validator_audit.jsonl`)

- verdicts: {'approve': 10, 'human_review': 1, 'revise': 1}; layers: {'deterministic': 6, 'specialist': 1, 'both': 5}
- top issue codes: [('specialist_needs_human', 1), ('unsupported_claim', 1)]

## PII scan of logs

- scanned 4 JSONL files: **0 unmasked sensitive values**  (target 0; `@example.com` synthetic addresses excluded from this count — they appear only where the customer's own email is part of ground-truth data)