# Debugging case studies — real incidents from building this system

Each case is a real fault that occurred during development, how the **traces / audit logs** exposed the root cause, the fix, and the regression test.
Tools used: `python scripts/trace_view.py` (span tree per query), `logs/validator_audit.jsonl`, `logs/security_events.jsonl`, `reports/eval_runs/*/results.jsonl`.

> **About the "4 hours → 15 minutes" claim.** I did not time these investigations, so I will not invent that number. To make it a number you can defend, run this protocol on 3–5 of the faults below (re-introduce each one): start a stopwatch with only plain application logs, record time-to-root-cause; repeat with the trace tree. Put the two measurements in the table at the bottom. The qualitative reason traces win is visible in every case: **one tree shows which stage, which model call and which tool the time/failure belongs to.**

---

## 1. A 27-second answer that was not our code's fault

* **Symptom** — a Technical-agent query took 33.9 s; the same query normally takes ~5 s.
* **Trace** — `agent.technical 33,853 ms → llm.technical.step0 27,519 ms (retries=0)`; every other span (tools, KB, later LLM steps) was < 1.2 s.
* **Root cause** — provider-side stall on the free NVIDIA endpoint (not retries, not rate limiting). Over 32 specialist calls: p50 1.2 s, p90 4.2 s, max 27.5 s ⇒ ≈3 % of calls stall for 20–30 s.
* **Fix** — **R-07 hedged requests**: if a call has not answered within a per-role delay (dispatcher 2.5 s, validator 3 s, safety 1.5 s, specialist 5 s) an identical request is raced; the first answer wins. Plus 20 s hard per-attempt timeout.
* **Lesson** — without nested spans this looks like "the agent is slow"; with them it is one line.

## 2. Agents making 4–5 LLM calls where 1 was enough

* **Symptom** — webhook query 23 s; billing query 18 s.
* **Trace** — `llm.technical.step0 … step4`, each ~30 output tokens, interleaved with `tool.search_knowledge_base` (reworded queries) and `tool.run_diagnostic`; later even after the KB tool was hidden the model kept calling it ("continuing the pattern" from the conversation).
* **Root cause** — the model re-searched/re-diagnosed instead of answering from the prefetched evidence.
* **Fix** — parallel **tool prefetch** (KB + logs + status, refund eligibility for the newest invoices) through the same guarded `execute_tool`; per-tool call caps; KB re-search tool hidden on a strong prefetch hit; **G-TOOL-12** only tools offered in the turn are executable; slimmer output schema; template merge (no merge LLM call; one merge call had taken 8.2 s for plain concatenation).
* **Result** — typical specialist: 1–2 LLM steps instead of 4–5.

## 3. Silent duplicate-id crash under concurrency

* **Symptom** — in the first mock evaluation at concurrency 8, two escalation cases returned `status=human_review` but had **no row** in `human_review_queue`; the reply was the generic "I'm passing your case to a human specialist" fallback text, not the normal holding reply with a reference id.
* **Trace/evidence** — result evidence showed `assign_to_human → source db:review:None` and a `tool.exception` event; reproducing the tool alone (sequentially) worked ⇒ a concurrency bug.
* **Root cause** — ids were `HRQ-{count(*)+1}`; two simultaneous requests computed the same id and one hit the primary-key constraint, which the runtime (by design) converts to "tool failed internally" so internals never leak to the model.
* **Fix** — **G-DATA-01** atomic `id_sequences` table with `UPDATE … RETURNING` (works on SQLite and Postgres) + retry on transient `database is locked`.
* **Regression test** — `test_concurrent_writes_get_unique_ids_no_failures` (30 parallel escalations + 30 parallel tickets, all ids unique).
* **Lesson** — this would have been a production incident at 100 concurrent users; the *evaluation harness* found it before the load test did. Swallowing exceptions for safety means the `tool.exception` trace event is the only clue — it is now one of the first things the report script counts.

## 4. Correct answers sent to human review (validator false rejects)

* **Symptom** — 3 of 9 end-to-end queries (double-charge refund, over-limit refund, 429 + cancel) ended in `human_review` after 2 revision rounds, 24–30 s each, although the specialist drafts were correct.
* **Evidence** — `logs/validator_audit.jsonl` rows with the judge's `unsupported_claims`: *"card ending in 4552"*, *"approval typically happens within 1 business day"*, and a log-derived sentence about 1000 requests/min that was in fact in the evidence.
* **Root causes (three)** — (a) the customer **profile** the specialist saw in its prompt was never passed to the validator as evidence, so "card ending in 4552" looked invented; (b) the judge's evidence block was cut at 6 500 characters **globally**, hiding the later items (the account logs); (c) the judge was pedantic about paraphrases ("typically" vs "usually").
* **Fix** — profile appended to evidence; per-item evidence budgets so every item is always shown; calibrated judge prompt (paraphrase is supported; only material, confident claims); `max_revisions` 2 → 1.
* **Result** — the same 9 queries: 0 unnecessary human reviews (the one remaining human-review was a correct abstention, see #7).

## 5. One specialist hallucinating another specialist's topic

* **Symptom** — "429 errors **and** how do I cancel" → revise → human review.
* **Evidence** — judge flagged *"Dashboard → Settings → Billing → Cancel Plan"* (a UI path that exists nowhere in the KB). It came from the **technical** specialist, which answered the whole message including the cancellation part.
* **Fix** — per-specialist **SCOPE** instruction in multi-intent messages ("answer only the technical part; the billing part is answered by another specialist; do not repeat apologies; do not invent steps for other topics"). The judge was right; the prompt was the bug.

## 6. The agent acting on its own initiative

* **Symptom** — a webhook question produced a support ticket (`TCK-000126`) although the prompt forbade it; an angry "charged twice again" message produced a refund the customer never asked for.
* **Root cause** — prompts are advice, not enforcement.
* **Fix** — **G-TOOL-10 intent-gated writes**: `create_refund_request` requires refund intent in the *customer's* message, `create_ticket` requires a ticket/follow-up request; otherwise the call is blocked, logged (`unrequested_write_blocked`) and the model is told to explain/offer or set `needs_human`.
* **Tests** — `test_refund_write_is_intent_gated_on_customer_message`, `test_ticket_write_is_intent_gated`.

## 7. Confident answer to an unanswerable question

* **Symptom** — "Does Orbit support Kafka connectors?" → *"Orbit does not currently offer Kafka… supports real-time ingestion via webhooks… build a custom bridge"* — none of it in the KB. In an earlier run a softer version ("the knowledge base does not mention Kafka") was **approved and cached**.
* **Evidence** — KB top-1 cosine for the Kafka query was 0.34 vs 0.35 for the answerable "what languages do your SDKs support": **retrieval score cannot separate answerable from unanswerable**.
* **Fix** — weak-match note in the KB tool result; hard rule "never claim the product does or doesn't support something unless an article says so"; **G-AGENT-05** uncertainty phrases force `needs_human`; the answer cache refuses weak/uncertain replies. The golden set now contains 24 unanswerable probes (`unanswerable` category) so this is *measured*, not assumed.

## 8. "Intermittent" test failures that were an event-loop artifact

* **Symptom** — live validator tests: `judge_unavailable` and `tool failed internally` on the 2nd/7th test but not when run alone.
* **Root cause** — the process-wide LLM gateway (and its HTTP pool) outlived each test's event loop. Production uses one loop, so this was a **test-harness** problem, not a product bug — worth proving before "fixing" product code.
* **Fix** — autouse fixture resets gateway/KB singletons per test and redirects logs to a temp dir.

## 9. Two environment gremlins

* **SQLite WAL copy** — a plain file copy of a WAL-mode DB sometimes missed the schema ⇒ tests failed with `no such table`. Fix: SQLite backup API in fixtures + `wal_checkpoint(TRUNCATE)` at the end of the seed.
* **Schema drift** — after adding a column, `create_all` silently kept the old table ⇒ `no such column conversations.channel_ref`. Fix: the seed rebuilds the schema; production needs Alembic migrations (listed in README).

## 10. The same truncation bug, twice (why my first "fix" didn't fix it)

* **Symptom** — after case #4's fix, a quick 40-case evaluation still sent *"What is your refund policy?"* to human review. `validator_audit.jsonl`: the judge called *"Refunds of $100 or less are approved automatically"* unsupported — a sentence copied from the KB.
* **Root cause** — the evidence handed to the validator was cut at 500 characters per KB chunk (in the specialist's response) and 420 (in the judge's view); the policy pages put the key bullets at the end. The deterministic `ungrounded_number: '15 minutes'` false positive had the same cause. The earlier "per-item budget" fix had only moved the cut, not removed it.
* **Fix** — carry the **full chunk** end to end; dedupe evidence and raise the total budget.
* **Effect** — quick-eval pass rate 75 % → 92.5 %; across the following fix rounds the unnecessary-human-review rate on answerable questions went 13.9 % → 6.1 % → 2.6 % (`reports/04_eval_full*.md`).
* **Lesson** — when an LLM judge "hallucinates", first diff *what the judge was shown* against the source. It was our input, not the judge.

## 11. Hedging that caused the overload it was meant to prevent

* **Symptom** — a 40-case run at concurrency 3 reported `hedges: 101` for 322 LLM calls (31 %), `rate_limited: 28`, `retries: 29`; queries took 20–70 s.
* **Root cause** — the hedge timer started when the request was *queued*, not when it was *sent*. Under load, calls waiting for the semaphore/token bucket were mistaken for stalled calls, duplicated, and pushed the rate limiter harder → more 429s → more waiting.
* **Fix** — the timer starts only after the request is on the wire. Regression tests: queue wait must not hedge; a stalled started request must.

## 12. The model says "I'll submit your refund" and doesn't

* **Symptom** — *"I want my money back for my last invoice"* → reply "I'll submit the request now" — no refund in the database. Another run appended `Confidence: 0.9, needs_human: false.` to the customer-visible text.
* **Root cause** — the model sometimes answers in prose instead of calling the write tool; and the prose fallback accepted the model's own metadata trailer.
* **Fix** — (a) deterministic **G-OUT-09** unfulfilled-promise check; (b) refunds became a **policy path** (**G-TOOL-11**): the dispatcher flags an explicit refund *request*, the prefetch runs the eligibility engine and files the request through the guarded tool; (c) trailer stripping + hygiene check; (d) the tool gate also requires the dispatcher's `refund_requested`, because the mock model filed a refund for *"what is your refund policy"* — a word-match gate was not enough.

## 13. Regexes defeated by typography

* **Symptom** — a false claim *"I’ve submitted a refund request"* was not flagged; true dates like `2026-10-01T06:00` *were* flagged as ungrounded; `10 s` / `14‑day` failed fact checks.
* **Root cause** — typographic apostrophes (U+2019), narrow no-break spaces (U+202F), non-breaking hyphens (U+2011), and a `\b` that cannot match between `1` and `T`.
* **Fix** — normalise model text (NFKC + a small translation table) at the source and in the checkers; ISO date regex uses digit look-arounds.

## 14. An evaluation that measured my own rate limiter

* **Symptom** — first full evaluation: 77.7 % pass, p50 latency 30 s, 109 human reviews, "68.7 % hallucination" from the independent judge.
* **Findings** — (a) 34 of 260 cases were `TimeoutError` fallbacks: concurrency 6 against a 70-rpm limiter queued calls past the 60 s request timeout; (b) the "independent" judge was mis-specified: unlabeled DB tuples, no customer name, no service status, no days-since-payment, so it flagged *true* statements; (c) several cases shared customers, so one case's refund polluted another's `no_refund` check; (d) a *real* policy over-trigger: any customer with ≥3 open tickets was escalated for any question (e.g. an SLA FAQ).
* **Fixes** — eval concurrency/timeout flags; judge reference rebuilt with labeled fields; disjoint customer pools; `repeat_contact` now needs a complaint/follow-up signal.
* **Lesson** — an evaluation is software: it needs its own review. The failure list with raw replies is what exposed (a)–(d); the aggregate number alone would have led to the wrong conclusions.

## 15. The primary model was retired mid-project (HTTP 410)

* **Symptom** — the last third of the final evaluation ran 3× slower; 111 of 260 cases touched `nemotron-3.5-lightning-30b` or `llama-3.2-11b`, 14 hit the request timeout, and the run was killed by its own time limit before writing results.
* **Evidence** — trace spans showed `model: …lightning-30b` instead of `…nemotron-3-super-120b`; a direct probe returned `410 Gone: the model 'nvidia/nemotron-3-super-120b-a12b' has reached its end of life on 2026-10-03T09:00:00Z`.
* **What worked** — the per-role **fallback chain** kept every customer answered (no 5xx) while the primary was gone.
* **What didn't** — every call still paid a round trip to the dead model first, and the fallbacks (flaky 30B, weaker 11B) were never validated as a primary. Evaluation numbers from the degraded window are not comparable, so those cases were re-run.
* **Fixes** — (a) **R-08 circuit breaker**: 404/410/403 mark a model dead for 15 min (`llm.model_unavailable` trace event, regression test with a fake 410); (b) re-probed the catalog: the only strong model served reliably is `nemotron-3-ultra-550b` (≈25 % transient 503 "overloaded", absorbed by retries) ⇒ new primary, fallbacks `llama-3.2-11b`, `gpt-oss-20b`; (c) the evaluation runner now writes each case's result as it finishes (a killed run no longer loses everything) and a merge tool combines re-runs.
* **Lesson** — free-tier catalogs change under you. Model names live in config, the fallback chain is tested, and every report states which model produced it.

## 16. A fallback that silently became the quality gate

* **Symptom** — on the new primary model the evaluation still showed ≈9 % unnecessary human reviews, on FAQs the KB answers verbatim ("What is the query timeout?" → *HTTP 504 after 30 s* was called unsupported). The same case judged in isolation: 3/3 clean.
* **Evidence** — correlating `validator_audit.jsonl` with the model recorded on each judge span: judge calls served by the **fallback** model (`llama-3.2-11b`, used when the primary returned repeated 503 "overloaded") rejected **68 %** of drafts (27 of 40); the primary rejected **8 %** (11 of 137). 15 % of judge calls had fallen back.
* **Root cause** — the fallback chain was designed for availability and applied to every role, including the quality gate. A weaker judge is not "degraded service", it is a different, miscalibrated judge.
* **Fix** — the validator and judge roles have **no fallback model** (more retries on the primary instead — transient 503s are cheap to retry); if the strong model stays unreachable the validator fails closed to a human. Availability-oriented fallbacks remain for dispatcher/specialist, where the validator re-checks the output.
* **Lesson** — record *which model produced each decision* in the trace; it turned a vague "validator is flaky" into a one-query diagnosis.

## 17. A card number that escaped through the LLM's own summary

* **Symptom** — the PII scan in `scripts/report.py` reported 3 hits on the final evaluation's logs. Two were false positives (the company's public `security@orbit.example`; a 16-digit random `span_id` that passes the Luhn check). One was **real**: `slack_outbox.jsonl` contained `4111 1111 1111 1111` inside the "Summary" of an escalation alert.
* **Root cause** — a customer pasted a card number; intake masked only API keys, so the escalation agent's LLM saw the full number, quoted it in its handoff summary, and the summary was stored in `human_review_queue` and posted to Slack. My Slack masking covered the raw `customer_message` field but not the LLM-written summary.
* **Fix** — mask card/SSN/IBAN/secret at **intake** (models never see them), and again at the sinks (`assign_to_human` before the DB write, Slack `review_blocks`). The scanner now ignores random ids and the company's own address.
* **Tests / evidence** — `test_full_card_number_is_masked_before_any_agent_sees_it`, `test_review_queue_never_stores_card_numbers_even_if_llm_summary_quotes_them`; a verification run (`reports/04_eval_pii_check.md`) scans clean: 0 unmasked values.
* **Lesson** — masking input fields is not enough once an LLM paraphrases them; scan the *outputs and sinks*, and make the scan part of the report.

---

## Time-to-root-cause worksheet (fill in yourself)

| # | fault | with plain logs (min) | with trace tree / audit log (min) |
|---|---|---|---|
| 1 | provider stall inside an agent run | | |
| 2 | agent tool loop | | |
| 3 | id collision under concurrency | | |
| 4 | validator false reject | | |
| 5 | cross-topic hallucination | | |

Only quote the ratio you actually measure.
