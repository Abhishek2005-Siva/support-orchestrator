"""Combine per-case results of several eval runs into ONE report (later runs override earlier ones per case id).
Used when part of a run was degraded by the provider (fallback model / timeouts) and only those cases were re-run.
python evals/merge_report.py --base full --rerun full_rerun --name final [--judge]"""
import argparse, asyncio, json, sqlite3, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.core.config import ROOT, get_settings
from evals.run_eval import build_report, stage_stats

ap = argparse.ArgumentParser(); ap.add_argument("--base", required=True); ap.add_argument("--rerun", nargs="*", default=[]); ap.add_argument("--name", default="final")
ap.add_argument("--base-file", default="results_rebuilt.jsonl"); a = ap.parse_args()
s = get_settings()
def load(run, fn=None):
    d = ROOT / "reports" / "eval_runs" / run
    f = d / (fn or "results.jsonl")
    if not f.exists(): f = d / "results_partial.jsonl"
    return {json.loads(l)["id"]: {**json.loads(l), "_run": run} for l in f.read_text().splitlines() if l.strip()}
merged = load(a.base, a.base_file if (ROOT / "reports/eval_runs" / a.base / a.base_file).exists() else None)
for r in a.rerun: merged.update(load(r))
results = list(merged.values())
stages, llm_calls, tokens = {}, {}, {}
import collections
stages = collections.defaultdict(list); llm_calls = collections.Counter(); tokens = collections.Counter()
for run in [a.base] + a.rerun:
    ids = {f"{run}-{r['id']}" for r in results if r["_run"] == run}
    st_, lc, tk = stage_stats(ROOT / "reports/eval_runs" / run / "logs" / "traces", ids)
    for k, v in st_.items(): stages[k] += v
    llm_calls.update(lc); tokens.update(tk)
judge_on = any(r.get("judge") for r in results)
note = f"_Merged from runs: {', '.join([a.base] + a.rerun)} — cases re-run after the provider degraded mid-run (calls served by a fallback model / graph timeouts): { sum(r['_run'] != a.base for r in results)} of {len(results)}._"
L = build_report(a.name, results, 0, s, {}, stages, llm_calls, tokens, judge_on, 4, note)
Path(s.report_dir, f"04_eval_{a.name}.md").write_text("\n".join(L))
(ROOT / "reports/eval_runs" / a.name).mkdir(exist_ok=True)
(ROOT / "reports/eval_runs" / a.name / "results.jsonl").write_text("\n".join(json.dumps({k: v for k, v in r.items() if k != "_run"}, default=str) for r in results))
print("\n".join(L[:36]))
