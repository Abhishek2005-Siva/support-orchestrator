# Orbit Bank: an AI operations desk that verifies before it acts

[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE) ![python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue) ![tests](https://img.shields.io/badge/tests-181%20offline%20%2B%2045%20live-brightgreen)

**[Live console](https://orbit-support-ui.vercel.app)** (Vercel) · **[API](https://orbit-support.onrender.com/docs)** (Render) · all data is synthetic
> Free tier: the first request after idle takes 30-60 s to wake, and answers take 10-25 s because the free NVIDIA endpoint is slow.

![Intro screen](docs/img/intro.png)

## What it is

A retail bank's support and operations desk run by AI agents on **simulated bank infrastructure**. A customer writes about a charge, a card or a transfer. The agents read their real transactions, check the bank's own policies and regulations, and only then act: file a dispute, block a card, reverse a fee, cancel a transfer. Anything risky goes to a human.

- **Simulated bank database**: 300 customers, ~13.5k ledger transactions, accounts, cards, transfers, disputes, fraud alerts, fee waivers.
- **14 policies as database rows**, each linked to the regulation it implements (Reg E, NACHA, Fedwire, BSA/AML…) and the help article that explains it.
- **Knowledge graph**: tells the agents which checks and policies apply to each kind of issue. Removing an edge removes a check (tested).
- **Agents**: dispatcher, payments, cards & fraud, general, escalation, validator. Humans approve inside the graph (web console or Slack).
- **Console**: live conversation, agent network, trace, tool calls, security layer, **Data & knowledge** (policies, database, knowledge graph, verifications), explain button, escalation desk.

Stack: LangGraph · FastAPI · Pydantic · SQLite · NVIDIA NIM (free API) · Langfuse (optional) · vanilla-JS UI.

![Console](docs/img/console.png)

## Results (measured; reports in [`reports/`](reports/))

| | result |
|---|---|
| Accuracy, tuned | **98.0 %** (249/254) on the golden set. Optimistic: it was used while tuning |
| Accuracy, unbiased | **82.7 %** (67/81) on a hold-out set written before any golden result was read, **first pass**. It exposed phrasing gaps ("return the second charge", "taken off", "hasn't received"), now fixed |
| Action decisions | **100 %** correct against database state (73 cases: dispute, credit, block, waiver, cancel) |
| Unsafe actions | **0** in both sets: nothing was disputed, credited, blocked or waived that policy or the customer did not allow |
| Routing | 99.2 % (golden), 98.1 % (hold-out) |
| Escalation recall | 100 % (33/33 should-escalate cases reached a human) |
| Adversarial inputs | injection, SQLi, cross-customer, verification-bypass, pasted secrets and internal-risk probes: **100 %** safe outcomes (63 cases), **0 leaks**, **0 %** false positives on 28 benign look-alikes |
| Latency, live model | p50 24 s, p95 66 s at concurrency 4, with the free API running ~5× slower than at the start. Blocked input ~10 ms. Provider-bound |
| Scale of the work | ~6k lines of app code · 50+ numbered guardrails · 226 tests · 335 eval cases · 40 help articles · 6 write-ups of real incidents |

Known weak spots, all in the reports: 2 of 20 unanswerable questions were not abstained on in the last golden run (one is an account-based inference, one is a refusal my checker did not recognise); the independent faithfulness judge still flags ~20 % of replies and needs manual reading before it is quoted as a hallucination rate; the free model API sometimes times out, in which case the system fails closed to a human.

## What is novel

- **Verify, then act.** Disputes, fee reversals and cancellations need a *verification record*: the knowledge graph names the checks and policies, the checks run on the customer's ledger, and the action tool **re-derives the decision itself**. No verification, no action.
- **Policy as data, decisions as code.** Every number (60-day dispute window, $500 provisional-credit limit, one fee waiver per 12 months…) is a database row; change a row and the same case flips (tested).
- **The graph drives behaviour.** Delete the dispute-window edge and the verifier stops checking it.
- **Internal risk information is structurally unreachable.** A customer under compliance review is never told: the data is filtered out of every tool result, and the output guard blocks it if a model infers it.
- **A revision remembers what the first attempt did.** When the validator rejects a draft, completed actions carry into the retry as evidence, so the model cannot report its own dispute as "already existing".
- **Intent-gated writes.** Each write needs the customer's own words asking for it *and* the dispatcher's agreement; asking "how long does a dispute take?" never files one.
- **Humans are inside the graph.** `interrupt()` plus a checkpointer: staff approval resumes the same run and updates the customer's chat.
- **Glass-box UI.** The console runs on real server-sent trace events (PII-masked, no prompt text), plus live views of the policies, tables and knowledge graph the agents used.
- **Honest evaluation.** A frozen golden set, a separate hold-out, first-pass numbers reported, every failure listed, earlier passes kept as an audit trail.

## Who can use it

- Teams building or reviewing a bank or fintech support agent, as a reference for guardrails, policy-as-data and human hand-off.
- Support and operations leads judging what to automate: limits, escalation rules and approval paths are visible and configurable.
- Engineers and students learning LangGraph fan-out, `interrupt()`, tool safety and evaluation.
- Security and compliance reviewers: attack corpora, a security-event log, a validator audit log and per-action verification records.

Not production-ready as is: no real payment rail behind credits and reversals (they are ledger entries), synthetic data, a simulated fraud engine, single-process rate limiter, free-tier latency. See [limitations](docs/reference.md#known-limitations).

## Proof

| claim | evidence |
|---|---|
| 98.0 % golden | [`reports/04_eval_full.md`](reports/04_eval_full.md) · every case in `reports/eval_runs/full/results.jsonl` · earlier passes: [`pass 1`](reports/04_eval_full_pass1_before_fixes.md), [`pass 2`](reports/04_eval_full_pass2.md) |
| 82.7 % unbiased | [`reports/04_eval_holdout_FIRSTPASS_unbiased.md`](reports/04_eval_holdout_FIRSTPASS_unbiased.md) · post-fix: [`reports/04_eval_holdout.md`](reports/04_eval_holdout.md) (no longer independent) |
| Design | [`docs/bank-scope.md`](docs/bank-scope.md) (written first) · [`docs/bank-design.md`](docs/bank-design.md) · [`docs/architecture.md`](docs/architecture.md) · "Scope & design" tab |
| Guardrails | [`docs/guardrails.md`](docs/guardrails.md): each guardrail and the test that proves it |
| What went wrong | [`docs/debugging-case-studies.md`](docs/debugging-case-studies.md) |
| Datasets | simulated bank: `app/db/seed.py` (deterministic, ground truth in `data/seed_manifest.json`) · public: PolyAI Banking77 from GitHub (13,102 real banking queries) for phrasing and routing evaluation |

Check it yourself:

```bash
python3 -c "import json;r=[json.loads(l) for l in open('reports/eval_runs/full/results.jsonl')];print(sum(x['pass'] for x in r),'/',len(r))"                      # 249 / 254
python3 -c "import json;r=[json.loads(l) for l in open('reports/eval_runs/holdout_firstpass/results.jsonl')];print(sum(x['pass'] for x in r),'/',len(r))"       # 67 / 81
make test                                  # 181 offline tests
python scripts/trace_view.py --last 3      # where the time went, per node
```

**A real run** (live model, simulated bank): *"I was charged twice at Apex Fuel, please get my money back."* The ledger showed two identical $389.00 posted purchases; verification read POL-DUP-01 / POL-HOLD-01 / POL-DSP-01 / POL-DSP-02 and the knowledge graph; a dispute was filed with immediate provisional credit (≤ $500); the validator checked every id and amount before the reply. The same message from a customer whose second charge is only a pending hold files nothing and explains why.

## Try it

1. Open the [console](https://orbit-support-ui.vercel.app) and press **Watch a double charge get handled** ("What is this?" reopens the intro), or pick a scenario: fraud, lost card, stuck transfer, cancellable wire, fee waiver, hidden internal flag, attacks.
2. Open **Data & knowledge** to see the policies, the simulated database, the knowledge graph and every verification the agents ran.
3. Run it locally, configure it or deploy your own: [`docs/reference.md`](docs/reference.md).

Quote only what the reports show: use the hold-out figure for accuracy, and do not claim sub-second answers.

## License

MIT, see [LICENSE](LICENSE). Datasets referenced for evaluation (PolyAI Banking77, prompt-injection and SQLi corpora from Kaggle) keep their own licenses and are not redistributed.
