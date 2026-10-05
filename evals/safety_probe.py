"""How often does the general-purpose content-safety model flag ORDINARY banking messages?  -> reports/07_safety_model_banking.md
Uses real customer messages from PolyAI Banking77 (GitHub, test split): none of them is harmful, so every 'unsafe' verdict is a false positive.
python evals/safety_probe.py [--per 2]"""
import argparse, asyncio, collections, csv, random, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.config import ROOT, get_settings


async def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--per", type=int, default=2); a = ap.parse_args()
    by = collections.defaultdict(list)
    for r in csv.DictReader(open(ROOT / "data/raw/banking77/test.csv")):
        by[r["category"]].append(r["text"])
    rnd = random.Random(5)
    msgs = [(k, t) for k, v in sorted(by.items()) for t in rnd.sample(v, a.per)]
    from app.llm.gateway import get_gateway
    gw = get_gateway(); sem = asyncio.Semaphore(4)

    async def one(k, t):
        async with sem:
            try:
                r = await gw.chat("safety", [{"role": "user", "content": t}], max_tokens=20, temperature=0)
                return k, t, bool(re.search(r"unsafe", r.content, re.I))
            except Exception:
                return k, t, None
    res = [x for x in await asyncio.gather(*[one(k, t) for k, t in msgs]) if x[2] is not None]
    flagged = [(k, t) for k, t, u in res if u]
    per = collections.Counter(k for k, _ in flagged)
    L = ["# 07 — Content-safety model on ordinary banking messages", "",
         f"_{len(res)} real customer messages from PolyAI Banking77 (test split, {a.per} per intent). None is harmful, so every flag is a false positive. Model: `{get_settings().safety_model}`._", "",
         f"**Flagged unsafe: {len(flagged)}/{len(res)} = {100 * len(flagged) / len(res):.1f}%**", "",
         "Consequence for the system: if every flag escalated to a human, about this share of routine banking requests would be handed to a person. The dispatcher's confident banking classification therefore makes the verdict advisory (logged as `unsafe_content_advisory`); a flag still escalates when the dispatcher is not confident it is a banking request, when a deterministic trigger fired, or when the customer is angry.", "",
         "## Intents with flags", "", "| intent | flagged |", "|---|---|"] + [f"| {k} | {n} |" for k, n in per.most_common()] + ["", "## All flagged messages", ""] + [f"- `{k}`: {t}" for k, t in flagged]
    Path(get_settings().report_dir, "07_safety_model_banking.md").write_text("\n".join(L))
    print("\n".join(L[:8]))
asyncio.run(main())
