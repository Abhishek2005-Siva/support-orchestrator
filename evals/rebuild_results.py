"""Rebuild results.jsonl of an eval run from its root trace spans + the preserved eval DB (used when a run was killed before writing results).
python evals/rebuild_results.py full       # reads reports/eval_runs/full/logs/traces, re-runs the deterministic checks against data/eval.db"""
import json, sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from evals.run_eval import ROOT, check_case

name = sys.argv[1]
run = ROOT / "reports" / "eval_runs" / name
cases = {json.loads(l)["id"]: json.loads(l) for l in (ROOT / "evals/golden.jsonl").read_text().splitlines() if l.strip()}
roots = {}
for f in sorted((run / "logs" / "traces").glob("*.jsonl")):
    for l in f.read_text().splitlines():
        r = json.loads(l)
        if r["name"] == "support_query" and r.get("trace_id", "").startswith(name + "-"):
            roots[r["trace_id"][len(name) + 1:]] = r
db = sqlite3.connect(ROOT / "data" / "eval.db")
baseline_tickets = {x[0] for x in db.execute("select id from tickets") if int(x[0].split("-")[1]) <= 125}
out = []
for cid, c in cases.items():
    r = roots.get(cid)
    if not r:
        continue
    o = r.get("output") or {}
    res = {"query_id": f"{name}-{cid}", "status": o.get("status", "error"), "reply": o.get("reply"), "intents": o.get("intents") or [], "latency_ms": r["duration_ms"],
           "flags": o.get("flags") or {}, "validation": o.get("validation")}
    fails = check_case(c, res, db, baseline_tickets, {})
    out.append({"id": cid, "category": c["category"], "customer_id": c["customer_id"], "message": c["message"], "status": res["status"], "reply": res["reply"],
                "intents": res["intents"], "latency_ms": res["latency_ms"], "flags": res["flags"], "validation": res["validation"], "fails": fails, "pass": not fails})
(run / "results_rebuilt.jsonl").write_text("\n".join(json.dumps(x, default=str) for x in out))
print(len(out), "of", len(cases), "cases rebuilt;", sum(x["pass"] for x in out), "pass")
