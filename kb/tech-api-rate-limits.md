---
id: tech-api-rate-limits
category: technical
title: API rate limits (429 errors)
---
# API rate limits (429 errors)

Limits are per workspace per minute: Free 60, Starter 300, Pro 600, Business 3,000, Enterprise 10,000 requests/min. When exceeded the API returns **429 Too Many Requests** (RATE_LIMIT_429) with a `Retry-After` header in seconds. Fix: back off using Retry-After with exponential backoff and jitter, batch requests, cache responses, or upgrade the plan for a higher limit. Response headers `X-RateLimit-Limit` and `X-RateLimit-Remaining` show your current budget.
