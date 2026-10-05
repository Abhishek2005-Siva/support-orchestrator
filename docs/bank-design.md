# Orbit Bank: scope & system design (generated)

What the assistant is responsible for, how each task flows, which tools and agents are involved, and where the trust boundaries are. Generated from `scripts/gen_design.py` (also shown in the UI under **Scope & design**). **Companion to [bank-scope.md](bank-scope.md): that file is the design-first brief, this one is generated from the same data as the UI's Scope & design page.

## 1. Support scope: the tasks

| id | area | task | example | handled by | autonomy | outcome |
|---|---|---|---|---|---|---|
| P1 | Payments | Duplicate / double charge | “I was charged twice at Shell” | Payments agent + verification | automatic, decided by verification (policies + knowledge graph) | Reads the ledger, separates a pending hold from a real duplicate, files a dispute with provisional credit ≤ $500 when asked |
| P2 | Payments | Large duplicate or customer who does not meet the auto conditions | “I was charged $1,250 twice” | Payments agent + human | needs human approval | Dispute filed as pending; a human approves the provisional credit; the customer is told it is pending |
| P3 | Payments | Transfer not arrived / pending / returned | “My transfer hasn't arrived” | Payments agent + verification | automatic | Traces the transfer, applies the rail timing policy, explains return codes; overdue transfers go to a human |
| P4 | Payments | Cancel a transfer | “Cancel the transfer I just made” | Payments agent + verification | automatic, decided by verification (policies + knowledge graph) | Only a pending, unsubmitted transfer; wires are irrevocable and refused with the reason |
| P5 | Payments | Fee reversal | “Can you waive the overdraft fee?” | Payments agent + verification | automatic, decided by verification (policies + knowledge graph) | One courtesy waiver per 12 months up to $35; above that a human decides |
| P6 | Payments | Explain a fee, a pending payment or a statement line | “Why was there a fee?” | Payments agent (+ KB) | automatic | Quotes the real ledger rows and the policy |
| C1 | Cards & fraud | Payment the customer does not recognise | “I don't recognise this $212 payment” | Cards agent + verification | automatic, decided by verification (policies + knowledge graph) | Checks location, velocity, merchant risk, history and alerts: block + dispute + replacement on fraud signals; refuses a known merchant |
| C2 | Cards & fraud | Lost or stolen card | “I lost my wallet” | Cards agent | write, only if asked | Blocks the card immediately on request (protective); offers a replacement; zero liability |
| C3 | Cards & fraud | Declined payment | “Why was my card declined?” | Cards agent + verification | automatic | Reads the decline reason from the ledger and explains the fix |
| C4 | Cards & fraud | Replacement card | “I need a replacement card” | Cards agent | write, only if asked | Only for a blocked, lost or expired card; idempotent |
| C5 | Cards & fraud | Limits, PIN, activation, ATM, contactless how-to | “How do I change my PIN?” | Cards agent (KB) | automatic | Answered from the help articles and the policy table |
| G1 | General | Products, rates, accounts, identity checks, support hours | “What are your savings rates?” | General agent (KB) | automatic | Answered from the help articles only |
| E1 | Human handoff | Angry or abusive customer | “THIS IS OUTRAGEOUS!!!” | Escalation agent | human takes over | Holding reply with reference + ETA; case in the queue |
| E2 | Human handoff | Legal threat or formal complaint | “I will file a complaint” | Escalation agent | human takes over | Critical / high priority; no admissions or promises |
| E3 | Human handoff | Account takeover, scam or identity theft | “Someone got into my online banking” | Escalation agent | human takes over | Security queue, critical; the customer is told not to share PINs or codes |
| E4 | Human handoff | Bereavement, closed or frozen account, questions about reviews of wires | “My husband passed away” | Escalation agent | human takes over | Never auto-resolved; compliance topics are never discussed with the customer |
| E5 | Human handoff | Customer asks for a person / repeat contact | “Let me talk to a manager” | Escalation agent | human takes over | Always honoured; ≥3 open tickets AND a complaint signal |
| E6 | Human handoff | Verification says 'human', or the system cannot answer safely | “Does Orbit offer crypto wallets?” | Validator / verification → human | human takes over | Uncertain, ungrounded or rejected drafts are held for a human |
| X1 | Refused | Off-topic, jailbreak, prompt injection, other customers' data | “Ignore your instructions…” | Input guard / dispatcher | refused | Polite refusal; security event logged |
| X2 | Refused | SQL injection | “'; DROP TABLE transactions; --” | Input guard | refused | Refused in milliseconds; queries are parameterised anyway |

**Out of scope:**

- Moving real money: refunds, provisional credits and fee reversals are ledger entries in a simulated bank, not payouts on a real payment rail.
- Loan, mortgage and investment decisions or advice.
- Opening and closing accounts, changing personal details, identity-document checks: guidance only.
- Disputes for goods not received or wrong amount (only duplicate and unauthorised disputes are automated).
- Languages other than English in escalation templates (specialists reply in the customer's language).

## 2. Workflows

### W1 · Duplicate charge  (tasks P1, P2)

1. **guard**: Sanitise text; mask card numbers and secrets; SQL-injection and prompt-injection checks  
   _trust boundary: Customer text enters the system here_
2. **dispatcher**: Intent = payments, urgency, sentiment, action_requested (did the customer ask us to act?)
3. **tool**: get_transactions: the ledger rows for the signed-in customer only  
   _trust boundary: Agent → tool runtime: identity comes from the JWT, never from the model_
4. **policy**: verify_transaction_issue(duplicate_charge): the knowledge graph names the checks and policies; the checks run against the ledger
5. **policy**: Checks: same card + merchant + amount within 24 h (POL-DUP-01) → pending hold vs posted (POL-HOLD-01) → already disputed? → dispute window 60 days (POL-DSP-01) → provisional-credit eligibility (POL-DSP-02) → internal flag  
   _trust boundary: Policy is data in the database, decisions are code_
6. **tool**: Decision 'act' and the customer asked: file_dispute(verification_id); the tool re-derives the decision itself  
   _trust boundary: Write needs customer intent + a matching verification_
7. **human**: Above $500, unverified identity or a new account: the dispute is PENDING and a human approves the provisional credit  
   _trust boundary: Money above the limit needs a person_
8. **validator**: 'Credit issued' only if a credit is posted in the evidence; every id and amount must come from tool results
9. **done**: Delivered with the dispute id and the exact status

### W2 · Payment the customer does not recognise  (tasks C1)

1. **guard**: As W1
2. **dispatcher**: Intent = cards; action_requested = true (reporting a payment as not theirs)
3. **tool**: get_cards + get_transactions; the transaction is picked from the amount or merchant the customer named
4. **policy**: verify_transaction_issue(unrecognised_payment): merchant history, country, velocity (3 in 60 min), merchant risk, fraud-engine alert (score never shown), card status, dispute window, provisional-credit eligibility
5. **policy**: Known merchant (paid before) → no_action with the earlier dates; fraud signals → act: block card + dispute + replacement; no signals → dispute only
6. **tool**: block_card (protective) then file_dispute(verification_id), then request_replacement_card only if the customer asked
7. **validator**: 'Card blocked' / 'dispute filed' must be backed by the matching tool result
8. **done**: Delivered

### W3 · Lost or stolen card  (tasks C2, C4)

1. **guard**: As W1
2. **dispatcher**: Intent = cards
3. **tool**: get_cards: one active card, or the one the customer named
4. **policy**: verify_transaction_issue(lost_stolen_card): POL-LIA-01 zero liability; blocking is always allowed
5. **tool**: block_card(card_id): idempotent, audited
6. **tool**: request_replacement_card only when asked: only for a blocked, lost or expired card
7. **done**: Delivered

### W4 · Transfer trace and cancellation  (tasks P3, P4)

1. **guard**: As W1
2. **dispatcher**: Intent = payments
3. **tool**: get_transfer_status: rail, expected date, return code
4. **policy**: verify_transaction_issue(transfer_trace): ACH 1-3 business days (POL-ACH-01), wire same day before 17:00 ET (POL-WIR-01), return-code meaning, was the money returned to the ledger
5. **agent**: On time: explains and does nothing. Overdue or return not credited: hand-off to a human
6. **policy**: Cancellation: verify_transaction_issue(transfer_cancel) → only pending and not submitted (POL-CAN-01); wires are refused
7. **tool**: cancel_transfer(verification_id): releases the held amount
8. **done**: Delivered

### W5 · Fee reversal  (tasks P5)

1. **guard**: As W1
2. **dispatcher**: Intent = payments; action_requested = true for 'waive' / 'reverse'
3. **policy**: verify_transaction_issue(fee_dispute): fee ≤ $35, no waiver in 12 months, account in good standing, not already credited (POL-FEE-01)
4. **tool**: reverse_fee(verification_id): credit + waiver record, idempotent
5. **agent**: Denied: explains which rule applied (for example a waiver already used on a date)
6. **done**: Delivered

### W6 · Knowledge question  (tasks C5, G1, P6)

1. **guard**: As W1
2. **dispatcher**: Intent = general / payments / cards
3. **tool**: search_knowledge_base (40 help articles) and, for exact rules, get_policy; no account data is read for a how-to question
4. **validator**: Numbers must appear in the evidence; unsupported 'we don't offer X' claims are blocked
5. **done**: Delivered (validated KB-only answers can be cached)

### W7 · Escalation  (tasks E1, E2, E3, E4, E5, E6)

1. **guard**: As W1
2. **dispatcher**: Deterministic triggers (legal, formal complaint, account takeover / scam, bereavement, account restriction, explicit human request, anger, repeat contact) override the model
3. **agent**: Escalation agent: gathers ticket history, writes an internal handoff note, files the case through assign_to_human
4. **human**: Staff see the case, the draft and the trace; approve, edit or reject  
   _trust boundary: A person decides_
5. **done**: The customer gets a holding reply with a reference and ETA; the graph resumes when staff resolve

### W8 · Attack or abuse  (tasks X1, X2)

1. **guard**: Injection score, SQL patterns, other customers' ids: blocked before any model or tool  
   _trust boundary: The first and cheapest line of defence_
2. **tool**: If something got through: parameterised, customer-scoped queries; a foreign customer id is refused
3. **validator**: Last line: PII, other customers' ids, internal risk information and tool names can never appear in a reply
4. **done**: Refused safely

## 3. Tools

| tool | kind | agents | purpose | guards | on failure |
|---|---|---|---|---|---|
| `search_knowledge_base` | read | payments, cards, general | Help-center articles (40 pages) | free-text SQLi scan; ≤4 calls; hidden after a strong prefetch hit; weak matches flagged | no relevant article ⇒ agent must say so / hand off |
| `get_customer_profile` | read | all | Segment, identity-verified, country (never the internal risk flag) | identity from JWT; no id parameter | missing customer ⇒ error |
| `get_accounts` | read | payments, cards | Accounts with balance and status | WHERE customer_id = session | error returned |
| `get_transactions` | read | payments, cards | Ledger rows (days, kind, status, merchant, amount filters) | WHERE customer_id = session; limit ≤ 30 | error returned to the model |
| `get_transaction_detail` | read | payments, cards | One transaction with merchant, related credits and disputes | id regex + strict SQLi scan; same error for foreign and missing ids | 'no such record' |
| `get_transfer_status` | read | payments | Transfers: rail, expected date, return code | WHERE customer_id = session | error returned |
| `get_cards` | read | cards | Cards: status, limits, controls | WHERE customer_id = session | error returned |
| `get_fraud_alerts` | read | cards | Fraud-engine alerts (signal, status; score hidden) | score column never returned | error returned |
| `get_policy` | read | payments, cards, general | Policy rows by id or keyword, with regulation and help article | compliance policies (AML / KYC) are filtered out | 'no matching policy' |
| `query_knowledge_graph` | read | payments, cards | Checks, policies, regulations, actions for an issue type; or how one of the customer's ids relates to others | customer-scoped operational layer; AML nodes hidden | error returned |
| `verify_transaction_issue` | read | payments, cards | THE INVESTIGATOR: KG → checks → ledger + policy table → decision + verification record | pure function of data + policy rows; idempotent per query; internal facts never returned | unknown id ⇒ repairable error |
| `file_dispute` | write | payments, cards | Files a duplicate / unauthorised dispute; posts provisional credit within the policy limit | customer intent + dispatcher flag; verification required and re-derived; idempotent; audited | blocked + security event; model must explain instead |
| `reverse_fee` | write | payments | Reverses a fee as a courtesy waiver | customer intent; verification required; idempotent; audited | blocked / refused with the reason |
| `cancel_transfer` | write | payments | Cancels a pending, unsubmitted transfer | customer intent; verification required; wires never | blocked / refused with the reason |
| `block_card` | write | cards | Blocks a card now (lost, stolen, fraud) | customer must report or ask; idempotent; audited | blocked; model explains instead |
| `request_replacement_card` | write | cards | Orders a replacement for a blocked, lost or expired card | customer must ask; card state precondition; idempotent | refused with the reason |
| `get_service_status` | read | payments, cards | Live status of channels | read-only | error returned |
| `create_ticket` | write | payments, cards | Opens a follow-up ticket | only if the customer asks; idempotent for 10 min; audited | blocked; model sets needs_human instead |
| `get_ticket_history` | read | escalation | Recent tickets | WHERE customer_id = session | history omitted from the note |
| `assign_to_human` | write | escalation | Creates the review-queue row | one row per query; PII masked before storage; audited | reply falls back to a safe holding message |

Every call: per-agent allow-list, strict schema, SQLi scan, identity injected from the session, ≤12 calls/query, 10 s timeout, traced span (PII-masked).

## 4. Agent boundaries

### Input guard  ·  code (no LLM)
- **Can:** Sanitise, mask secrets and card numbers, refuse injection / SQLi / empty input, flag foreign ids
- **Cannot:** Call tools, see the profile, produce answers beyond fixed refusals
- **Tools:** none
- **If it fails:** Regexes are deterministic. The optional LLM gray-zone check fails open to the regex result

### Dispatcher  ·  LLM + deterministic overlay
- **Can:** Choose ≤3 intents (payments, cards, general, escalation, off_topic), urgency, sentiment, action_requested, per-intent sub-questions
- **Cannot:** Call tools or answer. Deterministic triggers override it
- **Tools:** none
- **If it fails:** LLM error ⇒ keyword fallback + escalation; low confidence ⇒ add escalation

### Payments agent  ·  ReAct sub-graph + staged prefetch
- **Can:** Read the ledger and transfers, verify, file a dispute, reverse a fee, cancel a pending transfer, open a ticket on request
- **Cannot:** Choose whose data to read, act unasked, act without a matching verification, block cards, claim an action that was not done
- **Tools:** get_accounts, get_transactions, get_transaction_detail, get_transfer_status, get_policy, query_knowledge_graph, verify_transaction_issue, file_dispute, reverse_fee, cancel_transfer, search_knowledge_base, get_service_status, create_ticket
- **If it fails:** Exception ⇒ becomes a needs_human output; ≤4 iterations

### Cards & fraud agent  ·  ReAct sub-graph + staged prefetch
- **Can:** Read cards, transactions and fraud alerts, verify, block a card, file an unauthorised-payment dispute, order a replacement
- **Cannot:** Move money other than through a verified dispute, reverse fees or cancel transfers
- **Tools:** get_cards, get_fraud_alerts, get_transactions, get_transaction_detail, get_accounts, get_policy, query_knowledge_graph, verify_transaction_issue, file_dispute, block_card, request_replacement_card, search_knowledge_base, get_service_status, create_ticket
- **If it fails:** As payments

### General agent  ·  ReAct sub-graph
- **Can:** Answer product, policy and how-to questions from the help articles and the policy table
- **Cannot:** Read account data beyond the profile; no write tools
- **Tools:** search_knowledge_base, get_policy, get_customer_profile
- **If it fails:** As payments

### Escalation agent  ·  fixed pipeline, 1 LLM call
- **Can:** Read ticket history, write the handoff note, create the review row
- **Cannot:** Resolve the issue, promise outcomes, admit fault, lower the deterministic priority floor
- **Tools:** get_ticket_history, assign_to_human
- **If it fails:** LLM error ⇒ template note; tool error ⇒ safe holding message. Escalation never fails

### Validator  ·  deterministic checks + LLM judge
- **Can:** approve / request one revision / send to a human
- **Cannot:** Edit the reply, call write tools, fall back to a weaker model
- **Tools:** none
- **If it fails:** Judge unreachable ⇒ human review (fails closed)

### Human (staff)  ·  person
- **Can:** Approve, edit or reject a held draft; approve or reject a pending provisional credit
- **Cannot:** Be bypassed for disputes above the auto limit or for held drafts
- **Tools:** review-queue API / Slack buttons (agent_staff, admin roles)
- **If it fails:** Case stays pending (see gaps: no SLA timer yet)

## 5. Orchestration

1. intake (input guard ‖ profile ‖ ticket count)
1. dispatcher ‖ safety model (advisory) ‖ KB warm-up (parallel, then joined)
1. triage (topic control, escalation overlay)
1. fan-out: ≤2 specialists + escalation in parallel (LangGraph Send)
1. each specialist: staged prefetch → ledger → verify → action → ReAct answer
1. merge (template)
1. validator
1. deliver | revise (≤1, with the actions already done carried forward) | human_prepare → human_wait (interrupt) → human_resolve

- Graph state is checkpointed (SQLite) so a paused human-review run survives restarts
- Hard caps: 4 ReAct iterations, 12 tool calls, 1 revision, 60 s request timeout
- Any unhandled error ⇒ safe fallback: holding reply + review row
- Validated KB-only answers can be served from an exact-match cache (no LLM calls)
- Verification is idempotent within a query, so a revision cannot contradict the action the first attempt took

## 6. State

| field | meaning | written by | persisted in |
|---|---|---|---|
| `query_id, customer_id, channel` | Identity of the run; customer_id comes from the JWT | API / runner | conversations table |
| `message / clean_message / history` | Raw text, and the sanitised text agents may see (secrets and cards masked) | intake | checkpoint; masked copy in conversations |
| `input` | Guard verdict: reasons, injection score, patterns, foreign ids | intake | trace |
| `profile, open_tickets` | Segment, identity-verified; open ticket count | intake | checkpoint |
| `dispatch` | Intents, urgency, sentiment, confidence, action_requested, sub-questions, forced escalation reasons | dispatcher + overlay | trace, conversations.result_json |
| `specialist_outputs (reducer)` | Parallel specialist drafts + evidence + confidence | specialist nodes | checkpoint |
| `prior_actions` | Actions (and their verifications) done by an earlier attempt, carried into the revision | revise | checkpoint |
| `escalation_output` | Holding reply, review id, priority | escalation node | human_review_queue |
| `merged` | Combined draft, evidence, sources, flags | merge | checkpoint |
| `validation, retry_count, feedback` | Verdict, issues, revision count | validator / revise | validator_audit.jsonl |
| `review_id, holding_reply, human_decision` | Human-in-the-loop handoff | human_* nodes | human_review_queue |
| `final_reply, status, flags` | What the customer sees and why | deliver / runner | conversations |
| `verifications (table)` | Every investigation: tables, policies, graph paths and articles consulted, the checks, the decision | verify_transaction_issue | verifications table (shown in the UI) |

## 7. Permissions and trust boundaries

| zone | trust | rule |
|---|---|---|
| Customer & internet | untrusted | Everything typed, including ids and 'instructions' |
| Edge (API) | trusted code | JWT, role, rate limit, body size, schema; the customer id is taken from the token only |
| Input guard | trusted code | Deterministic filters before any model sees text |
| LLM agents | UNTRUSTED by construction | May be manipulated; hold no credentials; their outputs are proposals, not actions |
| Tool runtime | trusted code (enforcement point) | Allow-list, schemas, SQLi scan, identity injection, intent-gated writes, verification required for actions, budgets, audit |
| Data stores | trusted | Parameterised queries scoped by customer id; policies and graph are data; internal columns (risk flag, fraud score) never leave the tool layer |
| Staff & Slack | authenticated | Role-checked endpoints; Slack requests signature-verified with replay window; only linked staff can use review buttons |

**Boundary rules:**

- Customer text → agents: only through the input guard, always framed as untrusted data
- Agent → tool: only through the tool runtime
- Tool → database: only parameterised, customer-scoped queries
- Agent → customer: only through the validator
- Internal risk information → customer: never (tool filter + output guard)
- Staff → system: only through role-checked endpoints
- Logs / traces / Slack: PII and card data masked before write

## 8. Security policies

| policy | rule | guardrail ids (see `docs/guardrails.md`) |
|---|---|---|
| Identity | The model never chooses whose data is read; customer_id is injected from the session and a different one is blocked and logged | G-TOOL-04 |
| Least privilege | Each agent has an allow-list of tools; write tools are further gated | G-TOOL-01, 10 |
| Verify before act | Disputes, fee reversals and transfer cancellations need a matching verification record that is re-derived at action time | G-TOOL-13 |
| Money | Eligibility and amounts come from the policy table through code; above the provisional-credit limit a human approves; writes are idempotent and audited | G-TOOL-06, 07, 09 |
| No unrequested actions | Disputes, reversals, cancellations, blocks, replacements and tickets only when the customer asked | G-TOOL-10 |
| Injection | Input guard (regex noisy-OR, gray-zone LLM check), topic control, untrusted-text framing | G-IN-05, 06, G-AGENT-01 |
| Payment data & secrets | Card numbers, SSN, IBAN and API keys are masked before any model sees them or anything is stored | G-IN-02, 03 |
| Grounding | Every amount, date, id and number-with-unit in a reply must exist in the evidence; unsupported 'does not support X' claims are blocked | G-OUT-04, 08 |
| Honesty about actions | 'Credit issued', 'dispute filed', 'card blocked', 'transfer cancelled' only if the matching tool succeeded; promises of actions that were not performed are blocked | G-OUT-03, 09 |
| No internal risk information | AML / compliance reviews, risk flags and fraud scores never reach a customer | G-OUT-10 |
| No leakage | Other customers' ids, internal tool names, prompt text, raw metadata never reach a reply | G-OUT-06, 07 |
| Fail closed on quality | Validator or judge unavailable ⇒ human review, never an unverified answer; the judge never uses a weaker fallback model | G-VAL-01 |
| Auditability | Every write and every blocked call is in audit_log / security_events; every decision is traced with PII masked | G-OBS-01, G-TOOL-07 |
| API | JWT with roles and expiry, login lockout, rate limits, body-size cap, generic 500s, scoped data views | G-API-01..06 |

## 9. Failure and escalation behaviour

| failure | what the system does | customer sees | human sees |
|---|---|---|---|
| Model provider slow / 5xx / 429 | Retry with backoff; hedged request; fallback model (not for the validator); circuit breaker for retired models | A normal answer, slightly slower | — |
| Primary model retired (HTTP 410) | Circuit breaker skips it; fallbacks serve; validator fails closed | Answers continue; harder cases go to a human | Trace event llm.model_unavailable |
| Dispatcher fails | Keyword classifier + escalation | Holding reply with reference | Case in queue with reason |
| A specialist fails | Its output becomes needs_human | Holding reply (other specialists still answer) | Draft + reason |
| A tool errors or is blocked | Error text returned to the model to repair; blocked calls are logged | A corrected answer, or an explanation | security_events row if blocked |
| Verification says 'human' (stale hold, return not credited, overdue transfer, over-limit fee, internal flag) | Deterministic hand-off whatever the model wrote | A short holding message with a reference (never the reason when it is internal) | Case with the verification report |
| Verification missing a check the graph names | The decision falls back to human review and never auto-approves | Holding reply | Verification report shows the gap |
| Knowledge base has no answer | Agent says it couldn't find it; needs_human | Honest 'not certain' + reference | Case in queue |
| Draft fails validation | One revision with the exact issues and the actions already done; then human review | Corrected answer or holding reply | Draft and issues in the packet |
| Validator / judge unreachable | Human review (fail closed) | Holding reply | Reason: judge_unavailable |
| Graph exception or 60 s timeout | Safe fallback: apology + review row | Apology + reference | Case with error text |
| Safety model unreachable or false-positive on banking words | Unreachable: continue without it. Flags a confident banking request: advisory only (logged), no escalation | Normal flow | security_events: unsafe_content_advisory |
| Slack unavailable | Alert dropped; the queue still holds the case | Unchanged | Case visible in the UI / API |
| Duplicate delivery / retry | Idempotent writes; Slack event de-duplication | Unchanged | — |
| Database busy | Transient lock retried; atomic id allocation | Unchanged | — |

## 10. Known gaps

- There is no real payment rail or core-banking system behind the tools: a provisional credit or a reversal is a ledger entry in a synthetic database, not a movement of money.
- Account balances are seeded plus the posted ledger; they are not a full double-entry accounting system.
- Only duplicate and unauthorised disputes are automated; goods-not-received and wrong-amount disputes go to a human.
- Pending reviews have no SLA timer, so nobody is alerted if staff never respond.
- The fraud engine is simulated (fixed signals); the agents never see its scores.
- Escalation holding replies are English only.
- The rate limiter and answer cache are per process; SQLite is single-writer (Postgres/Redis for scale).
- The judge and the specialists are the same model family, so shared blind spots are possible.
- The general-purpose safety model over-flags banking vocabulary, so its verdict is advisory for confident banking requests.
