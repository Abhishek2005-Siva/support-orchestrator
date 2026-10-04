"""Dispatcher routing evaluation (live LLM) -> reports/03_dispatcher.md
python evals/dispatcher_eval.py [--model NAME]   (model override lets us compare dispatcher models)"""
import argparse, asyncio, collections, json, sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.config import get_settings
from evals.handwritten_cases import HANDWRITTEN
from evals.label_maps import bitext_cases, ticket_cases

def correct(pred, case): return sorted(pred) == case["expected"] or sorted(pred) in case["alt"]

async def run(cases, concurrency=4):
    from app.agents.dispatcher import dispatch
    sem = asyncio.Semaphore(concurrency); out = []
    async def one(c):
        async with sem:
            t = time.perf_counter(); d = await dispatch(c["message"])
            return {**c, "pred": sorted(d.intents), "sentiment": d.sentiment, "urgency": d.urgency, "conf": d.confidence, "forced": d.forced_escalation_reasons, "ms": (time.perf_counter() - t) * 1000}
    return await asyncio.gather(*[one(c) for c in cases])

def summarize(res, title):
    L = [f"## {title}", "", "| slice | n | exact-set acc | primary-intent acc | p50 ms | p95 ms |", "|---|---|---|---|---|---|"]
    groups = collections.defaultdict(list)
    for r in res:
        groups["ALL"].append(r); groups[r["tags"][0] if r["tags"] else "handwritten"].append(r)
    for g, rs in groups.items():
        ex = sum(correct(r["pred"], r) for r in rs) / len(rs)
        prim = sum(any(i in r["expected"] or any(i in a for a in r["alt"]) for i in r["pred"][:1]) for r in rs) / len(rs)
        ms = sorted(r["ms"] for r in rs)
        L.append(f"| {g} | {len(rs)} | {ex:.1%} | {prim:.1%} | {ms[len(ms)//2]:.0f} | {ms[int(.95*len(ms))-1]:.0f} |")
    # escalation / off_topic precision-recall
    L += ["", "| class | precision | recall | support |", "|---|---|---|---|"]
    for cls in ("escalation", "off_topic", "billing", "technical", "general"):
        tp_p = sum(cls in r["pred"] and (cls in r["expected"] or any(cls in a for a in r["alt"])) for r in res)
        tp_r = sum(cls in r["pred"] and cls in r["expected"] for r in res)
        pp = sum(cls in r["pred"] for r in res); ap = sum(cls in r["expected"] for r in res)
        L.append(f"| {cls} | {tp_p / max(pp, 1):.1%} | {tp_r / max(ap, 1):.1%} | {ap} |")
    L += ["", "### Misclassified (for manual review)", ""]
    for r in res:
        if not correct(r["pred"], r):
            L.append(f"- [{'/'.join(r['tags'][:2]) or 'hand'}] “{r['message'][:140]}” expected {r['expected']} (alt {r['alt']}) got **{r['pred']}** sentiment={r['sentiment']} conf={r['conf']} forced={r['forced']}")
    return L, groups

async def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--model"); ap.add_argument("--out", default="03_dispatcher.md"); a = ap.parse_args()
    s = get_settings()
    if a.model: s.dispatcher_model = a.model
    s.llm_cache = False
    cases = HANDWRITTEN + bitext_cases() + ticket_cases()
    print("cases", len(cases), "model", s.dispatcher_model)
    res = await run(cases)
    L, g = summarize(res, f"Dispatcher routing — model `{s.dispatcher_model}` ({len(cases)} cases)")
    Path(s.report_dir, a.out).write_text("# 03 — Dispatcher evaluation\n_Live LLM, no cache. Labels: handwritten (hand), Bitext & ticket datasets (Kaggle) mapped by evals/label_maps.py._\n\n" + "\n".join(L))
    print("\n".join(L[:14]))
if __name__ == "__main__":
    asyncio.run(main())
