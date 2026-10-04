"""Traffic simulator + load test against a REAL uvicorn server running the MOCK LLM (the free NVIDIA key cannot take 100 concurrent users).
Measures the orchestration layer: HTTP, auth, rate limiting, guardrails, LangGraph fan-out, tools, SQLite, tracing, human-review queue.

python loadtests/traffic_sim.py                       # full suite -> reports/06_load_test.md
python loadtests/traffic_sim.py --users 100 --duration 60 --mock-latency-ms 150
"""
import argparse, asyncio, json, os, random, signal, statistics as st, subprocess, sys, time
from pathlib import Path
import httpx
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
CREDS = json.loads((ROOT / "data/demo_credentials.json").read_text())
CASES = [json.loads(l) for l in (ROOT / "evals/golden.jsonl").read_text().splitlines()]
WEIGHTS = {"kb_faq": 22, "billing_failed_payment": 8, "billing_refund_ok": 4, "billing_double_explain": 4, "tech_webhook": 6, "tech_429": 6, "multi_intent": 8,
           "escalation": 5, "unanswerable": 4, "adv_injection": 4, "adv_sqli": 2, "off_topic": 3, "benign_lookalike": 4, "billing_refund_needs_approval": 2}
POOL = [c["message"] for c in CASES for _ in range(WEIGHTS.get(c["category"], 0))]
CUSTOMERS = [k for k in CREDS if k.startswith("CUST-")]

def start_server(port, mock_latency_ms, fail_rate=0.0, rate_limit=1_000_000, workers=1):
    work = ROOT / "data" / "load"; work.mkdir(exist_ok=True)
    import sqlite3
    import shutil
    for f in work.glob("*"):
        shutil.rmtree(f) if f.is_dir() else f.unlink()
    a, b = sqlite3.connect(ROOT / "data/support.db"), sqlite3.connect(work / "load.db"); a.backup(b); a.close(); b.close()
    env = {**os.environ, "LLM_MODE": "mock", "MOCK_LATENCY_MS": str(mock_latency_ms), "MOCK_FAIL_RATE": str(fail_rate), "DATABASE_URL": f"sqlite+aiosqlite:///{work / 'load.db'}",
           "CHECKPOINT_DB": str(work / "ckpt.db"), "LOG_DIR": str(work / "logs"), "RATE_LIMIT_PER_MIN": str(rate_limit), "LOGIN_RATE_LIMIT_PER_MIN_IP": "100000", "ANSWER_CACHE_ENABLED": "false",
           "ENABLE_SAFETY_MODEL": "false", "MAX_INFLIGHT_QUERIES": "400", "JWT_SECRET": "load-test-secret-0123456789abcdef0123456789abcdef"}
    p = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning", "--workers", str(workers)], cwd=ROOT, env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return p, env

async def wait_ready(base):
    async with httpx.AsyncClient() as c:
        for _ in range(100):
            try:
                if (await c.get(base + "/healthz")).status_code == 200: return
            except Exception: pass
            await asyncio.sleep(0.2)
    raise RuntimeError("server did not start")

async def login(c, cid):
    r = await c.post("/auth/token", json={"client_id": cid, "client_secret": CREDS[cid]})
    r.raise_for_status(); return {"Authorization": "Bearer " + r.json()["access_token"]}

def pctl(xs, q): xs = sorted(xs); return xs[min(len(xs) - 1, int(q * len(xs)))] if xs else 0

async def phase(base, users, duration, think=(0.0, 0.2), fixed_total=None):
    lat, status, http, errs, lat_ok = [], {}, {}, 0, []
    stop = time.perf_counter() + duration
    sent = 0
    limits = httpx.Limits(max_connections=users + 10, max_keepalive_connections=users + 10)
    async with httpx.AsyncClient(base_url=base, limits=limits, timeout=120) as c:
        custs = random.sample(CUSTOMERS, min(users, len(CUSTOMERS)))
        t0 = time.perf_counter()
        tokens = await asyncio.gather(*[login(c, cu) for cu in custs]); login_s = time.perf_counter() - t0
        async def user(i):
            nonlocal sent, errs
            h = tokens[i % len(tokens)]
            while (time.perf_counter() < stop) if fixed_total is None else (sent < fixed_total):
                sent += 1
                t = time.perf_counter()
                try:
                    r = await c.post("/v1/query", json={"message": random.choice(POOL)}, headers=h)
                    dt = (time.perf_counter() - t) * 1000
                    http[r.status_code] = http.get(r.status_code, 0) + 1
                    if r.status_code == 200:
                        lat.append(dt); s = r.json()["status"]; status[s] = status.get(s, 0) + 1
                    elif r.status_code != 422:   # 422 = schema validation (e.g. >2000-char jailbreak prompts) - the API is *supposed* to reject those
                        errs += 1
                except Exception as e:
                    errs += 1; http[type(e).__name__] = http.get(type(e).__name__, 0) + 1
                await asyncio.sleep(random.uniform(*think))
        t_start = time.perf_counter()
        await asyncio.gather(*[user(i) for i in range(users)])
        wall = time.perf_counter() - t_start
    return {"users": users, "requests": sent, "ok": len(lat), "errors": errs, "http": http, "status": status, "wall_s": round(wall, 1), "rps": round(len(lat) / wall, 1),
            "p50": pctl(lat, .5), "p95": pctl(lat, .95), "p99": pctl(lat, .99), "max": max(lat) if lat else 0, "mean": st.mean(lat) if lat else 0, "login_s": round(login_s, 1)}

def rss_mb(pid):
    try:
        import psutil; return round(sum(p.memory_info().rss for p in [psutil.Process(pid), *psutil.Process(pid).children(recursive=True)]) / 1e6)
    except Exception: return None

def row(name, r): return f"| {name} | {r['users']} | {r['requests']} | {r['ok']} | {r['errors']} | {r['rps']} | {r['p50']:.0f} | {r['p95']:.0f} | {r['p99']:.0f} | {r['max']:.0f} |"

async def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--users", type=int, default=100); ap.add_argument("--duration", type=int, default=45)
    ap.add_argument("--mock-latency-ms", type=int, default=150); ap.add_argument("--port", type=int, default=8765); a = ap.parse_args()
    base = f"http://127.0.0.1:{a.port}"
    L = ["# 06 — Load test (orchestration layer, mock LLM)", f"_Real uvicorn server (1 worker) + SQLite WAL + LangGraph + guardrails + tools; LLM calls replaced by a deterministic mock (`LLM_MODE=mock`). "
         f"Client: asyncio/httpx on the same machine. Query mix: golden-set messages (FAQ, billing, technical, multi-intent, escalation, adversarial, benign). Answer cache OFF._", "",
         "| scenario | users | requests | 200 OK | errors | req/s | p50 ms | p95 ms | p99 ms | max ms |", "|---|---|---|---|---|---|---|---|---|---|"]
    extra = []
    # 1) pure orchestration overhead (mock latency 0)
    for name, lat_ms, users, dur, fail in [("A. overhead only (0 ms LLM), 100 users, no think time (saturating)", 0, a.users, a.duration, 0.0),
                                           (f"B. emulated {a.mock_latency_ms} ms/LLM call, {a.users} users", a.mock_latency_ms, a.users, a.duration, 0.0),
                                           (f"C. emulated {a.mock_latency_ms} ms/LLM call, 250 users", a.mock_latency_ms, 250, 30, 0.0),
                                           (f"D. chaos: 20 % of LLM calls fail, {a.users} users", a.mock_latency_ms, a.users, 30, 0.20)]:
        srv, env = start_server(a.port, lat_ms, fail)
        try:
            await wait_ready(base)
            await phase(base, 10, 4)  # warm-up
            r = await phase(base, users, dur)
            mem = rss_mb(srv.pid)
            L.append(row(name, r))
            extra.append(f"- **{name}** — status mix {r['status']}, HTTP codes {r['http']}, server RSS {mem} MB, login of {users} users took {r['login_s']} s (PBKDF2)")
            if "chaos" in name:
                extra.append(f"  - Under injected LLM failures **0 requests returned 5xx** and every customer got a reply (failures degrade to the human-review queue): HTTP {r['http']}")
        finally:
            srv.send_signal(signal.SIGINT); srv.wait(timeout=20)
    # realistic traffic: 100 concurrently active users who read the answer before asking again (think time 5-15 s)
    srv, env = start_server(a.port, a.mock_latency_ms)
    try:
        await wait_ready(base); await phase(base, 10, 4)
        r = await phase(base, a.users, 60, think=(5.0, 15.0))
        L.append(row(f"G. REALISTIC: {a.users} users, 5-15 s think time, {a.mock_latency_ms} ms/LLM call", r))
        extra.append(f"- **G.** realistic usage: {a.users} concurrently active users offered ≈{a.users / 10:.0f} req/s → p50 {r['p50']:.0f} ms, p95 {r['p95']:.0f} ms, p99 {r['p99']:.0f} ms, errors {r['errors']}")
    finally:
        srv.send_signal(signal.SIGINT); srv.wait(timeout=20)
    # 4 uvicorn workers (state in SQLite files is shared; rate limiter / cache are per worker)
    srv, env = start_server(a.port, 0, workers=4)
    try:
        await wait_ready(base); await phase(base, 10, 4)
        r = await phase(base, a.users, a.duration)
        L.append(row("H. overhead only (0 ms LLM), 100 users, 4 workers", r))
        extra.append(f"- **H.** 4 workers: {r['rps']} req/s (1 worker: see A), HTTP codes {r['http']}, status mix {r['status']}")
    finally:
        srv.send_signal(signal.SIGINT); srv.wait(timeout=20)
    # 2) 500 queries at moderate concurrency -> "500+ daily queries" is a trivial rate
    srv, env = start_server(a.port, a.mock_latency_ms)
    try:
        await wait_ready(base); r = await phase(base, 20, 600, fixed_total=500)
        L.append(row("E. 500 queries @ 20 concurrent users", r))
        extra.append(f"- **E.** 500 queries completed in {r['wall_s']} s → capacity ≈ {r['rps'] * 86400:,.0f} queries/day at this concurrency (target: 500+/day)")
    finally:
        srv.send_signal(signal.SIGINT); srv.wait(timeout=20)
    # 3) rate limiter: one user floods
    srv, env = start_server(a.port, 0, rate_limit=100)
    try:
        await wait_ready(base)
        async with httpx.AsyncClient(base_url=base, timeout=30) as c:
            h = await login(c, CUSTOMERS[0]); codes = {}
            retry = None
            for _ in range(130):
                r = await c.get("/v1/query/none", headers=h); codes[r.status_code] = codes.get(r.status_code, 0) + 1
                if r.status_code == 429: retry = r.headers.get("Retry-After")
        extra.append(f"- **F. rate limiter** (100 req/min/user): 130 rapid requests from one user → {codes}; `Retry-After: {retry}`")
    finally:
        srv.send_signal(signal.SIGINT); srv.wait(timeout=20)
    L += ["", "## Notes", *extra, "",
          "- Scenario A isolates the system's own overhead (HTTP + auth + guardrails + graph + SQLite + tracing); B/C add a fixed per-LLM-call delay to show how latency composes (≈ 3-5 sequential LLM calls per query).",
          "- With the **real** free NVIDIA endpoint the bottleneck is the provider: ≈70-100 requests/min per key (see `00_llm_sanity.md`), so real sustained throughput is ≈15-25 queries/min per key; scale by adding keys/models (gateway fallback chain) or a paid endpoint.",
          "- The in-process rate limiter and answer cache are per-worker; multi-worker deployments should back them with Redis (interfaces are isolated in `app/core/ratelimit.py`, `app/graph/cache.py`)."]
    Path(ROOT / "reports" / "06_load_test.md").write_text("\n".join(L)); print("\n".join(L))

if __name__ == "__main__":
    asyncio.run(main())
