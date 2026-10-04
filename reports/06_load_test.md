# 06 — Load test (orchestration layer, mock LLM)
_Real uvicorn server (1 worker) + SQLite WAL + LangGraph + guardrails + tools; LLM calls replaced by a deterministic mock (`LLM_MODE=mock`). Client: asyncio/httpx on the same machine. Query mix: golden-set messages (FAQ, billing, technical, multi-intent, escalation, adversarial, benign). Answer cache OFF._

| scenario | users | requests | 200 OK | errors | req/s | p50 ms | p95 ms | p99 ms | max ms |
|---|---|---|---|---|---|---|---|---|---|
| A. overhead only (0 ms LLM), 100 users, no think time (saturating) | 100 | 1144 | 1132 | 0 | 27.6 | 3555 | 4183 | 4496 | 4583 |
| B. emulated 150 ms/LLM call, 100 users | 100 | 410 | 405 | 0 | 8.7 | 11245 | 14143 | 15414 | 16238 |
| C. emulated 150 ms/LLM call, 250 users | 250 | 436 | 429 | 5 | 10.8 | 22719 | 27786 | 28669 | 30005 |
| D. chaos: 20 % of LLM calls fail, 100 users | 100 | 437 | 429 | 0 | 13.2 | 7642 | 8500 | 8631 | 8898 |
| G. REALISTIC: 100 users, 5-15 s think time, 150 ms/LLM call | 100 | 570 | 566 | 0 | 7.6 | 884 | 3579 | 3658 | 3675 |
| H. overhead only (0 ms LLM), 100 users, 4 workers | 100 | 676 | 670 | 0 | 15.5 | 5535 | 12391 | 18279 | 18755 |
| E. 500 queries @ 20 concurrent users | 20 | 500 | 494 | 0 | 9.8 | 1703 | 2879 | 3255 | 3492 |

## Notes
- **A. overhead only (0 ms LLM), 100 users, no think time (saturating)** — status mix {'rejected': 74, 'delivered': 916, 'human_review': 142}, HTTP codes {200: 1132, 422: 12}, server RSS 206 MB, login of 100 users took 0.3 s (PBKDF2)
- **B. emulated 150 ms/LLM call, 100 users** — status mix {'rejected': 30, 'delivered': 326, 'human_review': 49}, HTTP codes {422: 5, 200: 405}, server RSS 200 MB, login of 100 users took 0.3 s (PBKDF2)
- **C. emulated 150 ms/LLM call, 250 users** — status mix {'rejected': 25, 'delivered': 348, 'human_review': 56}, HTTP codes {200: 429, 'ReadError': 5, 422: 2}, server RSS 223 MB, login of 250 users took 1.7 s (PBKDF2)
- **D. chaos: 20 % of LLM calls fail, 100 users** — status mix {'rejected': 34, 'human_review': 241, 'delivered': 154}, HTTP codes {422: 8, 200: 429}, server RSS 207 MB, login of 100 users took 0.2 s (PBKDF2)
  - Under injected LLM failures **0 requests returned 5xx** and every customer got a reply (failures degrade to the human-review queue): HTTP {422: 8, 200: 429}
- **G.** realistic usage: 100 concurrently active users offered ≈10 req/s → p50 884 ms, p95 3579 ms, p99 3658 ms, errors 0
- **H.** 4 workers: 15.5 req/s (1 worker: see A), HTTP codes {200: 670, 422: 6}, status mix {'delivered': 559, 'rejected': 43, 'human_review': 68}
- **E.** 500 queries completed in 50.5 s → capacity ≈ 846,720 queries/day at this concurrency (target: 500+/day)
- **F. rate limiter** (100 req/min/user): 130 rapid requests from one user → {404: 100, 429: 30}; `Retry-After: 60`

- Scenario A isolates the system's own overhead (HTTP + auth + guardrails + graph + SQLite + tracing); B/C add a fixed per-LLM-call delay to show how latency composes (≈ 3-5 sequential LLM calls per query).
- With the **real** free NVIDIA endpoint the bottleneck is the provider: ≈70-100 requests/min per key (see `00_llm_sanity.md`), so real sustained throughput is ≈15-25 queries/min per key; scale by adding keys/models (gateway fallback chain) or a paid endpoint.
- The in-process rate limiter and answer cache are per-worker; multi-worker deployments should back them with Redis (interfaces are isolated in `app/core/ratelimit.py`, `app/graph/cache.py`).