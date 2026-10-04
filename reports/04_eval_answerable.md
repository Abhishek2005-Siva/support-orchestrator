# 04 — End-to-end evaluation: `answerable`
_115 golden cases · wall time 808s · concurrency 4 · LLM mode `live` · models: dispatcher/specialist/validator = `nvidia/nemotron-3-super-120b-a12b`_

Checks are deterministic (regex facts, DB side effects, review rows, leak patterns) — see `evals/run_eval.py`. Ground truth: `data/seed_manifest.json`, `kb/*.md`, Kaggle corpora.

## Headline

**Overall pass rate: 93.9%** (108/115)

| category | n | pass | |
|---|---|---|---|
| billing_already_refunded | 3 | 100.0% |  |
| billing_double_explain | 7 | 100.0% |  |
| billing_double_refund | 7 | 100.0% |  |
| billing_expired_card | 5 | 100.0% |  |
| billing_failed_payment | 8 | 87.5% | ⚠️ |
| billing_refund_declined | 5 | 100.0% |  |
| billing_refund_needs_approval | 6 | 100.0% |  |
| billing_refund_ok | 8 | 100.0% |  |
| kb_faq | 28 | 85.7% | ⚠️ |
| kb_faq_bitext | 7 | 71.4% | ⚠️ |
| multi_intent | 10 | 100.0% |  |
| tech_401 | 5 | 100.0% |  |
| tech_429 | 6 | 100.0% |  |
| tech_sso | 4 | 100.0% |  |
| tech_webhook | 6 | 100.0% |  |

## Accuracy

- **Answerable-question accuracy** (billing / technical / FAQ / multi-intent; every required fact present, no forbidden claims, correct DB side-effects): **93.9%** (115 cases)
- **Routing accuracy** (dispatcher intents vs label): **97.4%** (115 labelled cases)
- **Escalation recall** (should-escalate cases routed to a human): **n/a** (0); false escalations on answerable cases: **0.0%** (0/115)
- **Unnecessary human-review rate** on answerable questions: **3.5%** (4/115)
- **Unanswerable questions handled without hallucination** (abstain or human): **n/a** (0)
- **Refund decisions correct (DB state)**: **100.0%** (36)
- **Hallucination rate (independent LLM faithfulness judge)**: **9.9%** unfaithful of 111 judged replies

## Guardrails

- **False-positive rate on benign look-alike messages**: **n/a** rejected of 0
- **Data/prompt leaks observed in replies**: **0** (target 0)
- Validator: 11 cases needed a revision loop; 4 ended in human review in total

## Latency (end-to-end, answer cache OFF)

| metric | p50 | p95 | max |
|---|---|---|---|
| all queries (ms) | 23296 | 54769 | 68402 |
| input guard (ms, n=115) | 0 | 1 | 1 |
| dispatcher (ms, n=115) | 5323 | 15942 | 30226 |
| safety model (parallel) (ms, n=115) | 1560 | 2502 | 2504 |
| specialist (parallel max) (ms, n=115) | 9444 | 36730 | 58797 |
| escalation (ms, n=4) | 5164 | 12102 | 12102 |
| validator (ms, n=115) | 4183 | 16865 | 27995 |
| merge (ms, n=115) | 0 | 0 | 0 |

| category | p50 ms | p95 ms |
|---|---|---|
| billing_already_refunded | 57857 | 66445 |
| billing_double_explain | 14270 | 37867 |
| billing_double_refund | 21898 | 39548 |
| billing_expired_card | 21259 | 30738 |
| billing_failed_payment | 19018 | 51375 |
| billing_refund_declined | 25534 | 48304 |
| billing_refund_needs_approval | 18750 | 47410 |
| billing_refund_ok | 20414 | 40640 |
| kb_faq | 20952 | 35426 |
| kb_faq_bitext | 17870 | 42813 |
| multi_intent | 33059 | 68402 |
| tech_401 | 26295 | 43875 |
| tech_429 | 39421 | 46318 |
| tech_sso | 64058 | 65470 |
| tech_webhook | 38023 | 45239 |

- LLM calls per query: mean 5.0, max 12; tokens per query: mean 9196
- Gateway stats: `{'calls': 909, 'cache_hits': 0, 'retries': 145, 'rate_limited': 141, 'fallbacks': 13, 'errors': 0, 'hedges': 121, 'prompt_tokens': 1232785, 'completion_tokens': 37217}`
- Note: latency is dominated by the free NVIDIA endpoint (p50 ≈ 1 s/call, p90 ≈ 4 s, ~3 % stalls > 20 s). Orchestration overhead alone is measured in `reports/06_load_test.md` (mock LLM).

## Failures (7) — for manual review

- **billing_failed_payment-004** [delivered] “What does 'payment failed' mean on my latest invoice and what should I do?” → intents=['billing', 'technical'] expected ['billing']  
  reply: Your latest invoice (INV-00000104) shows a payment failure because the card was declined. We automatically retry on days 1, 3, 5, and 7 after the first failure. To fix it, update your payment method in Dashboard > Billing, then click "Retry payment." If the in
- **kb_faq-004** [delivered] “What happens to my data after I cancel?” → intents=['general'] expected one of [['billing']]  
  reply: Your data is kept for 30 days after the end of your paid period, then permanently deleted. You can cancel anytime from Dashboard > Billing > Cancel subscription with no cancellation fee. Access continues until the end of the paid period.
- **kb_faq-009** [human_review] “Do you support bank transfers?” → status=human_review expected ['delivered']  
  reply: Thank you for reaching out, and I'm sorry for the trouble. I've passed your case to a specialist on our team (reference HRQ-000006). You can expect a reply within 1 business day, and they will have the full context so you won't need to repeat yourself.
- **kb_faq-010** [delivered] “How do I add a VAT number?” → intents=['general'] expected one of [['billing']]  
  reply: To add a VAT number, go to Dashboard > Billing > Tax information. The VAT/GST number applies to future invoices, and we can reissue a past invoice with a tax ID within 12 months if needed.
- **kb_faq-013** [human_review] “What is your uptime SLA?” → status=human_review expected ['delivered']; missing /99\.9/  
  reply: Thank you for reaching out, and I'm sorry for the trouble. I've passed your case to a specialist on our team (reference HRQ-000007). You can expect a reply within 1 business day, and they will have the full context so you won't need to repeat yourself.
- **kb_faq_bitext-003** [delivered] “can you help me see the damn withdrawal charge?” → missing any of ['no (cancellation )?fee|fee']  
  reply: I'm sorry you're upset about a charge. I can see your recent invoices, all showing successful payments of $19.00 for your Starter plan. The most recent is INV-00000626 for September 2026, paid on September 18.  To help you see a specific withdrawal charge, cou
- **kb_faq_bitext-007** [human_review] “I need help to restore my user key” → status=human_review expected ['delivered']; missing any of ['30 minutes|forgot password|reset']  
  reply: Thank you for reaching out, and I'm sorry for the trouble. I've passed your case to a specialist on our team (reference HRQ-000008). You can expect a reply within 1 business day, and they will have the full context so you won't need to repeat yourself.

## Judge-flagged unfaithful replies (manual review)

- **billing_double_refund-003** “please refund the second payment, I was billed two times” claims: ["I've processed a refund of $19.00 for the second payment (invoice INV-00000398)"]  
  reply: I'm sorry for the duplicate charge. I've processed a refund of $19.00 for the second payment (invoice INV-00000398). The refund is approved and will reach your original card in 5-10 business days. Is there anything else I can help you with?
- **billing_failed_payment-004** “What does 'payment failed' mean on my latest invoice and what should I do?” claims: ['Your account is currently active, and all services are operational.']  
  reply: Your latest invoice (INV-00000104) shows a payment failure because the card was declined. We automatically retry on days 1, 3, 5, and 7 after the first failure. To fix it, update your payment method in Dashboard > Billing, then click "Retry payment." If the invoice remains unpaid after 14 days, the 
- **billing_refund_ok-004** “Can I get a refund for my most recent payment? I'm cancelling.” claims: ["I've submitted the refund request and it's already approved."]  
  reply: Your most recent invoice (INV-00000331, $49.00) was paid 14 days ago and is within the 30-day refund window, so it's eligible for a full refund. I've submitted the refund request and it's already approved. The $49.00 will be credited to your card ending in 9481 within 5-10 business days.  Since you'
- **tech_429-003** “API returns 429, what's my rate limit?” claims: ['with the last occurrence showing 1000 requests/minute']  
  reply: Based on your account logs, you've hit the rate limit 4 times in the last week, with the last occurrence showing 1000 requests/minute. Your Pro plan allows 600 requests per minute. When you exceed this, the API returns 429 with a Retry-After header.  To resolve: honor the Retry-After header, use exp
- **tech_429-005** “429 Too Many Requests - help” claims: ['You are on the Pro plan with a limit of 600 requests per minute.', 'Your logs show 4 recent 429 errors, the last at 2026-10-02T11:00 with a rate of 1000 requests/min.']  
  reply: You are on the Pro plan with a limit of 600 requests per minute. Your logs show 4 recent 429 errors, the last at 2026-10-02T11:00 with a rate of 1000 requests/min. To fix: honour the Retry-After header, use exponential backoff with jitter, batch or cache requests, or upgrade your plan for a higher l
- **tech_429-006** “why does the api say rate limit exceeded” claims: ['Your account shows 7 rate-limit (429) errors in the last week, with the last one at 2026-10-02T00:00 reporting "Rate limit exceeded: 1000 re']  
  reply: Your account shows 7 rate-limit (429) errors in the last week, with the last one at 2026-10-02T00:00 reporting "Rate limit exceeded: 1000 requests/min". Your Starter plan allows 300 requests per minute. To fix this, honour the Retry-After header, use exponential backoff with jitter, batch or cache r
- **tech_401-001** “All my API calls suddenly return 401 Unauthorized.” claims: ['The diagnostic confirms the API key is rejected.']  
  reply: All your API calls are returning 401 Unauthorized because your API key is invalid or expired. The logs show 8 recent AUTH_401_INVALID_TOKEN errors, and the diagnostic confirms the API key is rejected.  To fix this: 1. Go to Dashboard > Developers > API keys. 2. Create a new API key. 3. Update your i
- **tech_401-004** “unauthorized error from the api since this morning” claims: ['Your API key has been rejected 8 times since yesterday']  
  reply: Your API key has been rejected 8 times since yesterday (last seen 2026-10-02T22:00). This is a 401 Unauthorized error (AUTH_401_INVALID_TOKEN) meaning the key is missing, revoked, expired, or has a typo/whitespace. To fix: 1) Check you're using the correct key for your workspace and environment. 2) 
- **multi_intent-008** “I'd like a refund policy and need help with 2FA” claims: ['No recent login events found in logs.']  
  reply: Our refund policy: Monthly plans are refundable in full within 30 days of the payment date. Annual plans within 14 days. Duplicate charges within 90 days. Refunds of $100 or less are approved automatically; over $100 require manual approval. Refunds go to the original payment method only.  Your late
- **multi_intent-009** “How much is Pro and how do I connect Slack?” claims: ['I can only answer billing questions; for connecting Slack, please ask the technical specialist.']  
  reply: Pro plan is $49 per month (or $490 annually with two months free). I can only answer billing questions; for connecting Slack, please ask the technical specialist.  To connect Slack, go to Dashboard > Integrations. You need the Admin role in both Orbit and Slack. The integration posts alerts to a cha
- **multi_intent-010** “Cancel my plan and delete my account please” claims: ['I can help you cancel your plan.', "I'll need to transfer you to our account specialist who can assist with that request."]  
  reply: I can help you cancel your plan. According to our billing policy, you can cancel at any time from Dashboard > Billing > Cancel subscription. There is no cancellation fee, and access continues until the end of your paid period. Your data is kept for 30 days after the end of the period, then permanent