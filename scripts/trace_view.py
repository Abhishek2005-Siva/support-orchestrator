"""Print span trees from logs/traces/*.jsonl so you can see exactly where time went.
usage: python scripts/trace_view.py [--last N] [--trace ID] [--root-name agent.technical] [--min-ms 0]
Spans without a trace id (unit/smoke runs) are grouped by their root span."""
import argparse, glob, json, sys
from collections import defaultdict
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--last", type=int, default=3); ap.add_argument("--trace"); ap.add_argument("--root-name"); ap.add_argument("--min-ms", type=float, default=0)
ap.add_argument("--dir", default=str(Path(__file__).resolve().parents[1] / "logs" / "traces"))
a = ap.parse_args()
recs = [json.loads(l) for f in sorted(glob.glob(a.dir + "/*.jsonl")) for l in open(f)]
by_id = {r["span_id"]: r for r in recs}
kids = defaultdict(list)
for r in recs: kids[r.get("parent_id")].append(r)
roots = [r for r in recs if r.get("parent_id") not in by_id]
if a.trace: roots = [r for r in roots if r.get("trace_id") == a.trace]
if a.root_name: roots = [r for r in roots if r["name"] == a.root_name]
roots = [r for r in roots if r["kind"] != "event"][-a.last:]

def show(r, depth=0, t0=None):
    extra = []
    md = r.get("metadata") or {}
    if r.get("model"): extra.append(r["model"].split("/")[-1])
    if r.get("usage"): extra.append(f'{r["usage"].get("prompt_tokens")}+{r["usage"].get("completion_tokens")}tok')
    if md.get("retries"): extra.append(f'retries={md["retries"]}')
    if md.get("cached"): extra.append("CACHED")
    if r["status"] != "ok": extra.append("ERR " + str(r.get("error", ""))[:60])
    if r["kind"] == "event": extra.append(json.dumps(md)[:100])
    print(f'{"  " * depth}{r["name"]:<{34 - 2 * depth}} {r["duration_ms"]:>8.0f} ms  [{r["kind"]}] {" ".join(extra)}')
    for c in sorted(kids.get(r["span_id"], []), key=lambda x: x["ts"]):
        if c["duration_ms"] >= a.min_ms: show(c, depth + 1)

for r in roots:
    print(f'\n=== trace {r.get("trace_id")} {r["ts"][:19]} ===')
    show(r)
