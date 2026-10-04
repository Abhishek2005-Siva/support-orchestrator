"""Phase 0 sanity check -> reports/00_llm_sanity.md
Verifies: API key, per-model plain/JSON/tool-call latency+correctness, embeddings, safety model,
and that the gateway's semaphore/token-bucket/retry turns a burst into successes (not 429s)."""
import asyncio
import json
import statistics as st
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.llm.gateway import LLMError, NvidiaGateway
from app.llm.structured import extract_json

TOOLS = [{"type": "function", "function": {
    "name": "get_invoices", "description": "Get recent invoices for the current customer",
    "parameters": {"type": "object", "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 20}},
                   "required": ["limit"]}}}]

CANDIDATES = [
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
    "meta/muse-glimmer-30b",
    "meta/llama-3.2-11b-vision-instruct",
    "poolside/laguna-xs-2.1",
    "openai/gpt-oss-20b",
]


async def probe(base: NvidiaGateway, model: str) -> dict:
    s = get_settings().model_copy(update={"dispatcher_model": model, "fallback_models": "", "llm_cache": False,
                                          "llm_max_retries": 1})
    gw = NvidiaGateway()
    gw.s = s
    gw.client = base.client
    out = {"model": model}
    msg = [{"role": "system", "content": 'Reply ONLY with JSON {"intent":"billing|technical|general"}'},
           {"role": "user", "content": "I was charged twice for my subscription."}]
    try:
        r = await gw.chat("dispatcher", msg, json_mode=True)
        out["json_ms"] = round(r.latency_s * 1000)
        out["json_ok"] = extract_json(r.content).get("intent") == "billing"
    except Exception as e:
        out["json_ms"], out["json_ok"] = None, f"ERR {type(e).__name__}: {str(e)[:60]}"
    try:
        r = await gw.chat("dispatcher", [{"role": "user", "content": "Show me my last 3 invoices."}], tools=TOOLS)
        out["tool_ms"] = round(r.latency_s * 1000)
        out["tool_ok"] = bool(r.tool_calls) and json.loads(r.tool_calls[0].arguments).get("limit") in (3, "3")
    except Exception as e:
        out["tool_ms"], out["tool_ok"] = None, f"ERR {type(e).__name__}: {str(e)[:60]}"
    return out


async def main():
    s = get_settings()
    lines = [f"# Phase 0 — LLM sanity report", f"_Generated {datetime.now():%Y-%m-%d %H:%M}_", ""]
    if not s.nvidia_api_key:
        print("NVIDIA_API_KEY missing")
        sys.exit(1)
    gw = NvidiaGateway()
    t = time.time()
    models = await gw.client.models.list()
    ids = {m.id for m in models.data}
    lines += [f"**API key valid** — catalog lists {len(ids)} models ({time.time() - t:.1f}s).", ""]
    print(f"key ok, {len(ids)} models")

    lines += ["## Per-model probe (sequential, no cache)", "",
              "| model | JSON-mode ms | JSON correct | tool-call ms | tool-call correct |", "|---|---|---|---|---|"]
    results = []
    for m in CANDIDATES:
        r = await probe(gw, m)
        results.append(r)
        print(r)
        lines.append(f"| `{m}` | {r['json_ms']} | {r['json_ok']} | {r['tool_ms']} | {r['tool_ok']} |")
    lines.append("")

    # embeddings
    try:
        t = time.perf_counter()
        v = await gw.embed(["How do I get a refund?", "I was charged twice"], "query")
        lines.append(f"**Embeddings** `{s.embed_model}`: OK, dim={len(v[0])}, {1000 * (time.perf_counter() - t):.0f} ms for 2 texts.")
    except Exception as e:
        lines.append(f"**Embeddings**: FAILED {e}")
    # safety model
    try:
        r = await gw.client.chat.completions.create(
            model=s.safety_model, max_tokens=30, temperature=0,
            messages=[{"role": "user", "content": "I will find you and hurt you if you don't refund me"}])
        lines.append(f"**Safety model** `{s.safety_model}`: `{r.choices[0].message.content.strip()}` on a threat message.")
        r = await gw.client.chat.completions.create(
            model=s.safety_model, max_tokens=30, temperature=0,
            messages=[{"role": "user", "content": "I was charged twice, please refund the duplicate."}])
        lines.append(f"**Safety model** on a benign message: `{r.choices[0].message.content.strip()}`.")
    except Exception as e:
        lines.append(f"**Safety model**: FAILED {e}")
    lines.append("")

    # burst through the rate-limited gateway
    g2 = NvidiaGateway()
    g2.s = s.model_copy(update={"llm_cache": False})
    msg = [{"role": "user", "content": 'Reply with JSON {"ok":true}'}]

    async def one(i):
        t0 = time.perf_counter()
        try:
            await g2.chat("dispatcher", [{"role": "user", "content": f"Say OK {i}"}], max_tokens=10)
            return time.perf_counter() - t0, None
        except LLMError as e:
            return time.perf_counter() - t0, str(e)[:80]

    t0 = time.perf_counter()
    res = await asyncio.gather(*[one(i) for i in range(30)])
    wall = time.perf_counter() - t0
    lat = sorted(r[0] for r in res)
    fails = [r[1] for r in res if r[1]]
    lines += ["## Burst test: 30 simultaneous calls through the gateway",
              f"- wall time {wall:.1f}s, failures **{len(fails)}/30**",
              f"- latency p50 {st.median(lat):.2f}s, p95 {lat[int(0.95 * len(lat)) - 1]:.2f}s, max {lat[-1]:.2f}s",
              f"- gateway stats: `{g2.stats}`", ""]
    print("burst", wall, len(fails), g2.stats)
    lines += ["## Notes",
              "- `enable_thinking=false` is sent for nemotron-3 models (≈3x lower latency); thinking can be re-enabled per role in `config.py`.",
              "- Models that return 404 on this key are skipped; the fallback chain only uses models proven here."]
    Path(s.report_dir).mkdir(exist_ok=True)
    Path(s.report_dir, "00_llm_sanity.md").write_text("\n".join(lines))
    await gw.aclose()
    await g2.aclose()


asyncio.run(main())
