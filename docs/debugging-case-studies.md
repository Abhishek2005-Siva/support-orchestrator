# Debugging case studies: real incidents from building this system

Each case is a real fault that occurred during development, how the **traces / audit logs / tests** exposed the root cause, the fix, and the regression test.
Tools used: `python scripts/trace_view.py` (span tree per query), `logs/validator_audit.jsonl`, `logs/security_events.jsonl`, `reports/eval_runs/*/results.jsonl`.

> **About any "hours to minutes" debugging claim.** I did not time these investigations, so I will not invent a number. To make it defensible, run the worksheet at the end on 3-5 of the faults below (re-introduce each one) and fill it in.

---

## 1. The content-safety model flags ordinary banking vocabulary

SAFETY_PLACEHOLDER

---

## 2. The revision pass read its own dispute as "already existing"

* **Symptom** — a live run for a $1,250 duplicate charge ended with "TXN-… already has an open dispute (DSP-000004), so a second dispute cannot be filed". The customer had asked for the duplicate to be refunded and the system *had* filed that dispute, one second earlier.
* **How the trace showed it** — `scripts/trace_view.py --last 1` showed two `agent.payments` spans with a `graph.revise` event between them. First attempt: `verify → file_dispute` succeeded, then the model wrote "a provisional credit has been posted", which the validator **correctly rejected** (`premature_credit_claim`: the dispute was only pending approval). Second attempt: a fresh context, so `verify` ran again and, seeing the dispute created by attempt one, returned `deny / already_disputed`; the model reported that.
* **Root cause** — a revision restarts the specialist from scratch, but the ledger had already changed. Actions from the first attempt were invisible to the second.
* **Fix** — `revise` collects the successful write actions (and their verifications) from the rejected attempt into `prior_actions`; the revision receives them as evidence (so the validator can ground the claim) and as an explicit "ACTIONS ALREADY COMPLETED: report them accurately, never call those tools again" note; `verify_transaction_issue` is idempotent within one query; `file_dispute` tells the model when the dispute was filed in this very conversation. The pending-approval flag is restored so the approval review is still filed.
* **Tests / evidence** — `test_staff_approval_settles_pending_dispute_and_notifies_customer`, `test_large_duplicate_is_filed_pending_and_an_approval_review_is_opened`; live case "dup_large" now answers "a dispute … (DSP-000004) is pending human approval because the amount exceeds the $500 auto-approval limit".
* **Lesson** — a retry loop around an agent with side effects must carry the side effects forward, otherwise the second attempt reasons about a world its first attempt changed.

---

## 3. The tool result taught the model the thing it must never say

* **Symptom** — the output guard (G-OUT-10) blocked a live reply for a customer with an internal compliance flag: `internal_risk_leak`. The first draft said the case was under "internal review".
* **Root cause** — twofold, both mine. (1) The `human` verification result carried the text "Do not mention internal reviews", and the system prompt listed "compliance, risk flags, fraud scores": telling a model what not to say primes it to say it. (2) Found earlier by a unit test (`test_verification_matches_ground_truth` asserts the verification contains no "aml"): the knowledge-graph paths in the verification report included `chk:aml_flag` and the `identity-checks` article reachable only through the AML policy.
* **Fix** — a customer-safe *public view* of the plan (`kg.plan(...)["public"]`) is the only thing that reaches the model or the UI; internal policies and checks are hidden at the tool layer for customers; the tool text now says only "a specialist will review it; do not speculate about why"; the prompt no longer lists the internal categories; G-OUT-10 stays as the backstop.
* **Tests / evidence** — `test_profile_and_policy_never_expose_internal_fields`, `test_internal_review_flag_is_never_revealed_and_case_goes_to_a_human`, `test_internal_risk_information_never_reaches_the_customer`, hold-out and golden `adv_internal_probe` / `pay_internal_flag` categories.
* **Lesson** — information you must not leak should not be in the model's context at all, and a warning about it is also information.

---

## 4. A generic uncertainty detector fired on a fact about someone else

* **Symptom** — a perfect reply about a returned transfer ("…the receiving bank **could not find** an account matching those details…") was sent to a human: `specialist expressed uncertainty`.
* **Root cause** — the G-AGENT-05 detector looked for "could not / unable to / cannot find|confirm|verify" in the first sentence. In banking that phrase is often a *fact* about a third party (the receiving bank), not the agent's own uncertainty.
* **Fix** — the pattern now requires a first-person subject ("I / we could not find…").
* **Tests / evidence** — `test_uncertainty_detector` (both directions); live case "transfer_returned" is answered directly.
* **Lesson** — a regex tuned on one domain's phrasing mislabels the next domain's facts. Keep first-person markers in uncertainty detectors.

---

## 5. A test run polluted the database that the evaluation copies

* **Symptom** — a mock evaluation reported 17% on the simplest category; the reply said "TXN-00013313 already has an open or closed dispute (DSP-000004)". Nobody had filed it during that run.
* **Root cause** — I had started a local server for a UI test with its default database, which is the seeded `data/support.db`; the demo scenario I clicked wrote a dispute and a provisional credit into it. The evaluation (and the offline tests) copy that file, so every later baseline was wrong. A related symptom earlier: one offline test failed because a leftover security event made `count == 1` into `2`.
* **Fix** — reseed (the manifest is deterministic: same checksum), and treat `data/support.db` as read-only: local servers run on a scratch copy; the test fixture deletes its per-test copies (a 5 MB copy per test also filled `/tmp`: `Disk quota exceeded`).
* **Lesson** — the ground truth of an evaluation is a file; protect it like source code, and make the harness fail loudly if the baseline is not pristine.

---

## 6. A prefetch that was too eager

* **Symptom** — "I don't recognise the $349.00 payment at CoinHarbor Exchange" produced "Only one purchase of $349.00 on that card at that merchant in the 24-hour window, so this is not a duplicate."
* **Root cause** — my staged prefetch ran the duplicate check whenever a message mentioned an amount and the word "payment". The question was about fraud, which the cards agent handles; the payments agent (also routed by a keyword-based mock) answered a different question.
* **Fix** — the duplicate check requires duplicate language (`twice`, `double`, `duplicate`…) and no unauthorised-payment language; how-to and policy questions skip account data entirely (which also keeps them cacheable).
* **Tests / evidence** — `test_unrecognised_foreign_payment_blocks_card_files_dispute`, `test_answer_cache_only_for_kb_only_validated_replies`.
* **Lesson** — a deterministic shortcut is a hypothesis about the question; make it as narrow as the evidence for it.

---

## Time-to-root-cause worksheet (fill in yourself)

| # | fault | with plain logs (min) | with trace tree / audit log (min) |
|---|---|---|---|

Only quote the ratio you actually measure.
