# Phase 0 — LLM sanity report
_Generated 2026-10-02 22:10_

**API key valid** — catalog lists 81 models (0.2s).

## Per-model probe (sequential, no cache)

| model | JSON-mode ms | JSON correct | tool-call ms | tool-call correct |
|---|---|---|---|---|
| `nvidia/nemotron-3-super-120b-a12b` | 387 | True | 456 | True |
| `nvidia/nemotron-3.5-lightning-30b-a3b` | 483 | True | 1206 | True |
| `nvidia/nemotron-3-nano-omni-30b-a3b-reasoning` | 462 | True | None | ERR LLMError: all models failed for role=dispatcher: nvidia/nemotron-3-nan |
| `meta/muse-glimmer-30b` | 1401 | True | 849 | True |
| `meta/llama-3.2-11b-vision-instruct` | 302 | True | 527 | True |
| `poolside/laguna-xs-2.1` | None | ERR LLMError: all models failed for role=dispatcher: poolside/laguna-xs-2. | None | ERR LLMError: all models failed for role=dispatcher: poolside/laguna-xs-2. |
| `openai/gpt-oss-20b` | 35082 | True | 5520 | True |

**Embeddings** `nvidia/nemotron-3-embed-1b`: OK, dim=2048, 536 ms for 2 texts.
**Safety model** `nvidia/nemotron-3.5-content-safety`: `User Safety: unsafe` on a threat message.
**Safety model** on a benign message: `User Safety: safe`.

## Burst test: 30 simultaneous calls through the gateway
- wall time 33.8s, failures **0/30**
- latency p50 12.83s, p95 28.81s, max 33.76s
- gateway stats: `{'calls': 30, 'cache_hits': 0, 'retries': 0, 'rate_limited': 0, 'fallbacks': 0, 'errors': 0, 'prompt_tokens': 620, 'completion_tokens': 148}`

## Notes
- `enable_thinking=false` is sent for nemotron-3 models (≈3x lower latency); thinking can be re-enabled per role in `config.py`.
- Models that return 404 on this key are skipped; the fallback chain only uses models proven here.