"""Observability report from local traces (what the Langfuse dashboards would show) -> reports/05_observability.md
python scripts/report.py [--dir logs] [--name 05_observability]
Sections: latency p50/p95 per node, LLM usage, tool error rate, guardrail events, human-review rate, validator verdicts, PII scan of the logs."""
import argparse, collections, glob, json, statistics as st, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.guardrails.pii import find_pii

ap = argparse.ArgumentParser(); ap.add_argument("--dir", default=str(Path(__file__).resolve().parents[1] / "logs")); ap.add_argument("--name", default="05_observability")
a = ap.parse_args()
d = Path(a.dir)
recs = [json.loads(l) for f in sorted(glob.glob(str(d / "traces" / "*.jsonl"))) for l in open(f)]
spans = [r for r in recs if r["kind"] != "event"]
events = [r for r in recs if r["kind"] == "event"]
pc = lambda xs, q: sorted(xs)[min(len(xs) - 1, int(q * len(xs)))] if xs else 0
L = [f"# 05 — Observability report", f"_Source: `{d}/traces` · {len(spans)} spans · {len({r['trace_id'] for r in spans if r.get('trace_id')})} traces_", ""]

roots = [r for r in spans if r["name"] == "support_query"]
if roots:
    lat = [r["duration_ms"] for r in roots]
    status = collections.Counter(((r.get("output") or {}).get("status")) for r in roots)
    L += ["## Queries", "", f"- {len(roots)} queries; latency p50 {pc(lat, .5):.0f} ms, p95 {pc(lat, .95):.0f} ms, max {max(lat):.0f} ms",
          f"- outcomes: {dict(status)}", f"- **human-review rate**: {100 * status.get('human_review', 0) / len(roots):.1f}% · rejected at input: {100 * status.get('rejected', 0) / len(roots):.1f}%"]
    conf = [r["scores"].get("validator_confidence") for r in roots if r.get("scores") and r["scores"].get("validator_confidence") is not None]
    if conf: L.append(f"- validator confidence: mean {st.mean(conf):.2f}, p10 {pc(conf, .1):.2f}")
    L.append("")

by_name = collections.defaultdict(list)
for r in spans:
    key = r["name"]
    if key.startswith("llm."): key = "llm." + key.split(".")[1]
    if key.startswith("agent.") and key.split(".")[1] in ("payments", "cards", "general"): key = "agent.specialist"
    by_name[(r["kind"], key)].append(r["duration_ms"])
L += ["## Latency per node / span (ms)", "", "| kind | span | n | p50 | p95 | max |", "|---|---|---|---|---|---|"]
for (k, n), v in sorted(by_name.items(), key=lambda kv: -sum(kv[1]))[:30]:
    L.append(f"| {k} | {n} | {len(v)} | {pc(v, .5):.0f} | {pc(v, .95):.0f} | {max(v):.0f} |")

llm = [r for r in spans if r["kind"] == "llm"]
if llm:
    pt = sum((r.get("usage") or {}).get("prompt_tokens", 0) for r in llm); ct = sum((r.get("usage") or {}).get("completion_tokens", 0) for r in llm)
    mods = collections.Counter(r.get("model") for r in llm)
    L += ["", "## LLM usage", "", f"- {len(llm)} generations · {pt:,} prompt + {ct:,} completion tokens · per query ≈ {len(llm) / max(len(roots), 1):.1f} calls, {(pt + ct) / max(len(roots), 1):,.0f} tokens",
          f"- models: {dict(mods)}", f"- retried: {sum(1 for r in llm if (r.get('metadata') or {}).get('retries'))} · fallback model used: {sum(1 for r in llm if (r.get('metadata') or {}).get('fallback_used'))} · cache hits: {sum(1 for r in llm if (r.get('metadata') or {}).get('cached'))}"]

tools = collections.defaultdict(lambda: [0, 0, 0])
for r in spans:
    if r["kind"] == "tool":
        t = tools[r["name"]]; t[0] += 1
        o = r.get("output") or {}
        if o.get("blocked"): t[2] += 1
        elif not o.get("ok", True): t[1] += 1
L += ["", "## Tool calls", "", "| tool | calls | errors (bad args / not found) | blocked by guardrail |", "|---|---|---|---|"]
for n, (c, e, b) in sorted(tools.items(), key=lambda kv: -kv[1][0]):
    L.append(f"| {n} | {c} | {e} ({100 * e / c:.0f}%) | {b} |")

sec = collections.Counter(r["name"] for r in events if r["name"].startswith(("security.", "cache.", "graph.", "llm.structured", "llm.final", "specialist.", "validator.", "dispatcher.", "escalation.")))
sf = d / "security_events.jsonl"
if sf.exists():
    se = [json.loads(l) for l in sf.read_text().splitlines()]
    L += ["", "## Security events (`logs/security_events.jsonl`)", "", f"{dict(collections.Counter(e['kind'] for e in se))}"]
L += ["", "## Notable events", "", f"{dict(sec)}"]

va = d / "validator_audit.jsonl"
if va.exists():
    rows = [json.loads(l) for l in va.read_text().splitlines()]
    L += ["", "## Validator decisions (`logs/validator_audit.jsonl`)", "", f"- verdicts: {dict(collections.Counter(r['verdict'] for r in rows))}; layers: {dict(collections.Counter(r['layer'] for r in rows))}",
          f"- top issue codes: {collections.Counter(i['code'] for r in rows for i in r['issues']).most_common(8)}"]

# PII scan of every log file (G-IN-03: nothing sensitive may be written)
hits = collections.Counter(); files = 0
for f in d.rglob("*.jsonl"):
    files += 1
    for line in f.read_text().splitlines():
        line = __import__("re").sub(r'"(?:span_id|parent_id|trace_id)": "[^"]*"', "", line)   # ids are random hex/digits, not PII
        for m in find_pii(line):
            if m.kind in ("email", "card", "ssn", "secret", "iban") and not m.value.endswith(("@example.com", "@orbit.example")):
                hits[m.kind] += 1
L += ["", "## PII scan of logs", "", f"- scanned {files} JSONL files: **{sum(hits.values())} unmasked sensitive values** {dict(hits) or ''} (target 0; `@example.com` synthetic addresses excluded from this count — they appear only where the customer's own email is part of ground-truth data)"]
(Path(__file__).resolve().parents[1] / "reports" / f"{a.name}.md").write_text("\n".join(L))
print("\n".join(L[:60]))
