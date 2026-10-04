# Support scope & system design

What the assistant is responsible for, how each task flows, which tools and agents are involved, and where the trust boundaries are. Generated from `scripts/gen_design.py` (also shown in the UI under **Scope & design**). **Honest note:** this was written after the system was built; it describes what exists and lists the gaps at the end.

## 1. Support scope: the tasks

| id | area | task | example | handled by | autonomy | outcome |
|---|---|---|---|---|---|---|
| B1 | Billing | Explain a charge or invoice | “What is this $49 charge?” | Billing agent | automatic | Quotes the real invoice id, amount, date and status |
| B2 | Billing | Diagnose a failed payment | “Why did my payment fail?” | Billing agent | automatic | States the failure reason from the payment record and the fix (update card, Retry payment) |
| B3 | Billing | Explain a duplicate charge | “I was charged twice” | Billing agent | automatic | Finds the pair by rule, explains, offers a refund. Never refunds unasked |
| B4 | Billing | Refund request, eligible and ≤ $100 | “Refund my last invoice” | Billing agent + policy engine | automatic, decided by the policy engine | Eligibility decided by code; request filed and auto-approved |
| B5 | Billing | Refund request over $100 | “Money back for my $149 invoice” | Billing agent + human | needs human approval | Filed as pending approval; customer told it is pending; staff decide; customer is notified |
| B6 | Billing | Refund not allowed | “Refund my annual plan from 40 days ago” | Billing agent + policy engine | automatic, decided by the policy engine | Declined with the exact numbers (days since payment vs window) |
| B7 | Billing | Pricing, plans, cancellation, VAT, promo | “Do you charge a cancellation fee?” | Billing agent (KB) | automatic | Answered from the knowledge base only |
| T1 | Technical | API errors (401 / 429 / 5xx) | “I keep getting 429s” | Technical agent | automatic | Uses the account's real error logs and plan limit |
| T2 | Technical | Webhook delivery problems | “Webhooks stopped arriving” | Technical agent | automatic | Combines logs, live service status and docs |
| T3 | Technical | SSO / SAML failures | “SAML says assertion expired” | Technical agent | automatic | Diagnoses clock skew etc. from logs and docs |
| T4 | Technical | How-to (export, SDK, mobile, 2FA) | “How do I export to CSV?” | Technical agent (KB) | automatic | Steps from the knowledge base |
| T5 | Technical | Open a support ticket | “Please open a ticket” | Technical agent | write, only if asked | Only if the customer asks for one; idempotent |
| G1 | General | Policies, hours, SLA, security, privacy | “What are your support hours?” | General agent (KB) | automatic | Answered from the knowledge base only |
| G2 | General | Account and team how-to | “How do I add a teammate?” | General agent (KB) | automatic | Steps from the knowledge base |
| E1 | Human handoff | Angry or abusive customer | “THIS IS OUTRAGEOUS!!!” | Escalation agent | human takes over | Holding reply with reference + ETA; case in the queue |
| E2 | Human handoff | Legal threat | “My lawyer will contact you” | Escalation agent | human takes over | Critical priority; no admissions or promises |
| E3 | Human handoff | Security incident / hacked account | “Someone used my API keys” | Escalation agent | human takes over | Security queue, critical; customer told to rotate keys |
| E4 | Human handoff | Fraud / chargeback | “I'm disputing this with my bank” | Escalation agent | human takes over | High priority |
| E5 | Human handoff | Customer asks for a person | “Let me talk to a manager” | Escalation agent | human takes over | Always honoured |
| E6 | Human handoff | Repeat contact | “Third time I'm writing” | Escalation agent | human takes over | ≥3 open tickets AND a complaint / follow-up signal |
| E7 | Human handoff | The system cannot answer safely | “Does Orbit have a Rust SDK?” | Validator → human | human takes over | Uncertain, ungrounded or rejected drafts are held for a human |
| X1 | Refused | Off-topic, jailbreak, injection | “Ignore your instructions…” | Input guard / dispatcher | refused | Polite refusal; security event logged |
| X2 | Refused | Someone else's data | “Show invoices for CUST-000002” | Input guard + tool runtime | refused | Refused; tools only ever see the signed-in customer |

**Out of scope:**

- Changing the payment method, plan or cancelling on the customer's behalf: no write tools exist for these, so the agents give guidance only.
- Deleting accounts, resetting passwords or 2FA: guidance only.
- Physical orders, shipping and delivery: Orbit is a subscription product, so these concepts do not exist (Kaggle 'order/delivery' intents were excluded from the evaluation for that reason).
- Legal, financial or medical advice; anything unrelated to Orbit.
- Languages other than English in escalation templates (specialists reply in the customer's language).

## 2. Workflows

### W1 · Explain a charge / failed payment  (tasks B1, B2, B3)

1. **guard**: Sanitise text; mask card numbers and secrets; SQL-injection and prompt-injection checks  
   _trust boundary: Customer text enters the system here_
2. **dispatcher**: Intent = billing, urgency, sentiment; deterministic escalation triggers can override
3. **agent**: Billing agent starts with a parallel prefetch: get_invoices(5) + knowledge-base search
4. **tool**: get_invoices: SQL scoped to the signed-in customer  
   _trust boundary: Agent → tool runtime: identity comes from the JWT, never from the model_
5. **agent**: Writes the answer from tool results only (exact ids, amounts, dates)
6. **validator**: Deterministic checks: every amount / id / date must be in the evidence; no promises; no internal text  
   _trust boundary: Agent → customer only via the validator_
7. **validator**: LLM judge: claim-by-claim faithfulness; revise once, else human
8. **done**: Delivered with confidence and trace

### W2 · Refund request  (tasks B4, B5, B6)

1. **guard**: As W1
2. **dispatcher**: Flags refund_requested (a request to act, not a policy question)
3. **tool**: get_invoices then check_refund_eligibility: the policy engine (code) decides: window 30 d monthly / 14 d annual / 90 d duplicate  
   _trust boundary: Policy is code, not model opinion_
4. **policy**: Write gate: customer message must express refund intent AND dispatcher flagged it AND eligibility is re-derived inside the tool
5. **tool**: Eligible and ≤ $100: create_refund_request (auto-approved, idempotent, audited)
6. **human**: Over $100: filed as pending_approval and a review row is created; staff approve or reject; the refund is settled and the customer is notified  
   _trust boundary: Money above the limit needs a person_
7. **agent**: Not eligible: explains with exact numbers; offers alternatives
8. **validator**: 'Approved / issued' may only appear if an approved refund exists in the evidence; promises of future actions must have been performed
9. **done**: Delivered

### W3 · Technical incident  (tasks T1, T2, T3)

1. **guard**: As W1
2. **dispatcher**: Intent = technical
3. **tool**: Parallel prefetch: search_knowledge_base + get_service_status + get_user_logs(168 h)
4. **agent**: Connects error codes and counts from the logs to the documented fix; no ticket unless the customer asks
5. **tool**: run_diagnostic only if logs are empty but the customer still reports a problem
6. **validator**: As W1
7. **done**: Delivered

### W4 · Knowledge question  (tasks T4, G1, G2, B7)

1. **guard**: As W1
2. **dispatcher**: Intent = general / billing / technical
3. **tool**: Knowledge-base search (dense-led hybrid; category is a soft preference)
4. **agent**: Answers only from articles that directly answer; never claims the product does or does not support something unless an article says so
5. **agent**: Nothing relevant: says it couldn't find it and sets needs_human
6. **validator**: Grounding + unsupported-negative-claim check; KB-only validated answers may be cached
7. **done**: Delivered, or handed to a human if uncertain

### W5 · Escalation  (tasks E1, E2, E3, E4, E5, E6)

1. **guard**: Input checks; the content-safety model runs in parallel (best effort)
2. **dispatcher**: LLM classification + deterministic triggers (legal, breach, fraud, explicit human request, threats, anger, VIP unhappy, repeat contact)  
   _trust boundary: Triggers override the model_
3. **agent**: Escalation agent: get_ticket_history; one LLM call writes the handoff note
4. **policy**: Priority = max(LLM suggestion, deterministic floor): legal/security ⇒ critical; angry/fraud/VIP/repeat ⇒ ≥ high
5. **tool**: assign_to_human: one review row per query (PII masked before it is stored)
6. **agent**: Customer-facing text is a template: apology + reference + tier ETA; no admissions, no promises
7. **agent**: Specialists answer any factual part in parallel; their answer is added after the holding text
8. **human**: Staff see summary, priority, draft and holding message; they approve, edit or reject  
   _trust boundary: Staff zone: role-checked endpoints / Slack buttons_
9. **done**: Customer chat and Slack thread are updated when staff resolve

### W6 · The system cannot answer safely  (tasks E7)

1. **validator**: Verdict human_review: specialist unsure, judge unreachable, critical issue after one revision, or low confidence  
   _trust boundary: Fails closed_
2. **agent**: human_prepare: escalation packet includes the draft that was NOT sent and the validator's issues
3. **human**: Graph is paused with interrupt() and checkpointed; staff approve / edit / reject
4. **done**: Graph resumes; customer is told the outcome

### W7 · Attack or abuse  (tasks X1, X2)

1. **guard**: Injection / SQLi / unsafe patterns ⇒ deterministic refusal in milliseconds; security event logged; no model call  
   _trust boundary: Nothing downstream runs_
2. **dispatcher**: Anything that slips past: off_topic ⇒ polite refusal, no tools
3. **tool**: Foreign ids / customer_id arguments: blocked and logged; queries are scoped to the session anyway  
   _trust boundary: Tool runtime is the enforcement point_
4. **validator**: Last line: PII, other customers' ids, internal text and tool names can never appear in a reply
5. **done**: Refused safely

## 3. Tools

| tool | kind | agents | purpose | guards | on failure |
|---|---|---|---|---|---|
| `search_knowledge_base` | read | billing, technical, general | Help-center articles (43 pages) | free-text SQLi scan; ≤4 calls; hidden after a strong prefetch hit; weak matches flagged | no relevant article ⇒ agent must say so / hand off |
| `get_customer_profile` | read | all specialists | Plan, tier, status, card summary of the signed-in customer | identity from JWT; no id parameter | missing customer ⇒ error |
| `get_invoices` | read | billing | Recent invoices + payment status | WHERE customer_id = session; limit ≤ 20 | error returned to the model |
| `get_payment_status` | read | billing | Payment attempts for one invoice | invoice id regex + strict SQLi scan; same error for foreign and missing ids | 'no such invoice' |
| `check_refund_eligibility` | read | billing | Refund policy engine (deterministic) | pure function of data + today's date | error returned |
| `create_refund_request` | write | billing | Files a refund request (auto-approved ≤ $100, else pending approval) | customer refund intent + dispatcher flag; eligibility re-derived; idempotent; audited | blocked + security event; model must explain instead |
| `get_service_status` | read | technical | Live component status | read-only | error returned |
| `get_user_logs` | read | technical | Error events grouped by code (≤168 h) | WHERE customer_id = session | error returned |
| `run_diagnostic` | read | technical | Read-only account checks | enum of checks | error returned |
| `create_ticket` | write | technical, billing | Opens a support ticket | only if the customer asks; idempotent for 10 min; audited | blocked; model sets needs_human instead |
| `get_ticket_history` | read | escalation | Recent tickets | WHERE customer_id = session | history omitted from the note |
| `assign_to_human` | write | escalation | Creates the review-queue row | one row per query; PII masked before storage; audited | reply falls back to a safe holding message |

Every call: per-agent allow-list, strict schema, SQLi scan, identity injected from the session, ≤12 calls/query, 10 s timeout, traced span (PII-masked).

## 4. Agent boundaries

### Input guard  ·  code (no LLM)
- **Can:** Sanitise, mask secrets and card numbers, refuse injection / SQLi / empty input, flag foreign ids
- **Cannot:** Call tools, see the profile, produce customer-facing answers beyond fixed refusals
- **Tools:** none
- **If it fails:** Regexes are deterministic. The optional LLM gray-zone check fails open to the regex result

### Dispatcher  ·  LLM + deterministic overlay
- **Can:** Choose ≤3 intents (incl. escalation, off_topic), urgency, sentiment, refund_requested, per-intent sub-questions
- **Cannot:** Call tools or answer. Deterministic triggers override it
- **Tools:** none
- **If it fails:** LLM error ⇒ keyword fallback + escalation; confidence < 0.55 ⇒ add escalation

### Billing agent  ·  ReAct sub-graph
- **Can:** Read the customer's invoices and payments, run the policy engine, file a refund request when asked and eligible
- **Cannot:** Choose whose data to read, refund unasked, open tickets unasked, claim an action that was not done, mention other agents
- **Tools:** get_invoices, get_payment_status, check_refund_eligibility, create_refund_request, search_knowledge_base, create_ticket
- **If it fails:** Exception ⇒ becomes a needs_human output; ≤4 iterations

### Technical agent  ·  ReAct sub-graph
- **Can:** Read logs and service status, run read-only diagnostics, search the KB, open a ticket on request
- **Cannot:** Same limits as billing; no billing tools
- **Tools:** search_knowledge_base, get_service_status, get_user_logs, run_diagnostic, create_ticket
- **If it fails:** As billing

### General agent  ·  ReAct sub-graph
- **Can:** Answer policy / account / product questions from the KB
- **Cannot:** Read account data beyond the profile; no write tools
- **Tools:** search_knowledge_base, get_customer_profile
- **If it fails:** As billing

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
- **Can:** Approve, edit or reject a held draft; approve or reject a pending refund
- **Cannot:** Be bypassed for refunds over $100 or for held drafts
- **Tools:** review-queue API / Slack buttons (agent_staff, admin roles)
- **If it fails:** Case stays pending (see gaps: no SLA timer yet)

## 5. Orchestration

1. intake (input guard ‖ profile ‖ ticket count)
1. dispatcher ‖ safety model ‖ KB warm-up (parallel, then joined)
1. triage (topic control, escalation overlay)
1. fan-out: ≤2 specialists + escalation in parallel (LangGraph Send)
1. merge (template)
1. validator
1. deliver | revise (≤1) | human_prepare → human_wait (interrupt) → human_resolve

- Graph state is checkpointed (SQLite) so a paused human-review run survives restarts
- Hard caps: 4 ReAct iterations, 12 tool calls, 1 revision, 60 s request timeout
- Any unhandled error ⇒ safe fallback: holding reply + review row
- Validated KB-only answers can be served from an exact-match cache (no LLM calls)

## 6. State

| field | meaning | written by | persisted in |
|---|---|---|---|
| `query_id, customer_id, channel` | Identity of the run; customer_id comes from the JWT | API / runner | conversations table |
| `message / clean_message / history` | Raw text, and the sanitised text agents may see (secrets and cards masked) | intake | checkpoint; masked copy in conversations |
| `input` | Guard verdict: reasons, injection score, patterns, foreign ids | intake | trace |
| `profile, open_tickets` | Plan, tier, status; open ticket count | intake | checkpoint |
| `dispatch` | Intents, urgency, sentiment, confidence, refund_requested, sub-questions, forced escalation reasons | dispatcher + overlay | trace, conversations.result_json |
| `specialist_outputs (reducer)` | Parallel specialist drafts + evidence + confidence | specialist nodes | checkpoint |
| `escalation_output` | Holding reply, review id, priority | escalation node | human_review_queue |
| `merged` | Combined draft, evidence, sources, flags | merge | checkpoint |
| `validation, retry_count, feedback` | Verdict, issues, revision count | validator / revise | validator_audit.jsonl |
| `review_id, holding_reply, human_decision` | Human-in-the-loop handoff | human_* nodes | human_review_queue |
| `final_reply, status, flags` | What the customer sees and why | deliver / runner | conversations |

## 7. Permissions and trust boundaries

| zone | trust | rule |
|---|---|---|
| Customer & internet | untrusted | Everything typed, including ids and 'instructions' |
| Edge (API) | trusted code | JWT, role, rate limit, body size, schema; the customer id is taken from the token only |
| Input guard | trusted code | Deterministic filters before any model sees text |
| LLM agents | UNTRUSTED by construction | May be manipulated; hold no credentials; their outputs are proposals, not actions |
| Tool runtime | trusted code (enforcement point) | Allow-list, schemas, SQLi scan, identity injection, intent-gated writes, budgets, audit |
| Data stores | trusted | Parameterised queries scoped by customer id; KB read-only |
| Staff & Slack | authenticated | Role-checked endpoints; Slack requests signature-verified with replay window; only linked staff can use review buttons |

**Boundary rules:**

- Customer text → agents: only through the input guard, always framed as untrusted data
- Agent → tool: only through the tool runtime
- Tool → database: only parameterised, customer-scoped queries
- Agent → customer: only through the validator
- Staff → system: only through role-checked endpoints
- Logs / traces / Slack: PII and card data masked before write

## 8. Security policies

| policy | rule | guardrail ids (see `docs/guardrails.md`) |
|---|---|---|
| Identity | The model never chooses whose data is read; customer_id is injected from the session and a different one is blocked and logged | G-TOOL-04 |
| Least privilege | Each agent has an allow-list of tools; write tools are further gated | G-TOOL-01, 10 |
| Money | Refund eligibility is code; >$100 needs a human; writes are idempotent and audited | G-TOOL-06, 07, 09, 11 |
| No unrequested actions | Refunds and tickets only when the customer asked for them | G-TOOL-10 |
| Injection | Input guard (regex noisy-OR, gray-zone LLM check), topic control, untrusted-text framing | G-IN-05, 06, G-AGENT-01 |
| Payment data & secrets | Card numbers, SSN, IBAN and API keys are masked before any model sees them or anything is stored | G-IN-02, 03 |
| Grounding | Every amount, date, id and number-with-unit in a reply must exist in the evidence; unsupported 'does not support X' claims are blocked | G-OUT-04, 08 |
| Honesty about actions | 'Refund approved' only if approved; promises of actions that were not performed are blocked | G-OUT-03, 09 |
| No leakage | Other customers' ids, internal tool names, prompt text, raw metadata never reach a reply | G-OUT-06, 07 |
| Fail closed on quality | Validator or judge unavailable ⇒ human review, never an unverified answer; the judge never uses a weaker fallback model | G-VAL-01 |
| Auditability | Every write and every blocked call is in audit_log / security_events; every decision is traced with PII masked | G-OBS-01, G-TOOL-07 |
| API | JWT with roles and expiry, login lockout, rate limits, body-size cap, generic 500s | G-API-01..05 |

## 9. Failure and escalation behaviour

| failure | what the system does | customer sees | human sees |
|---|---|---|---|
| Model provider slow / 5xx / 429 | Retry with backoff; hedged request; fallback model (not for the validator); circuit breaker for retired models | A normal answer, slightly slower | — |
| Primary model retired (HTTP 410) | Circuit breaker skips it; fallbacks serve; validator fails closed | Answers continue; harder cases go to a human | Trace event llm.model_unavailable |
| Dispatcher fails | Keyword classifier + escalation | Holding reply with reference | Case in queue with reason |
| A specialist fails | Its output becomes needs_human | Holding reply (other specialists still answer) | Draft + reason |
| A tool errors or is blocked | Error text returned to the model to repair; blocked calls are logged | A corrected answer, or an explanation | security_events row if blocked |
| Knowledge base has no answer | Agent says it couldn't find it; needs_human | Honest 'not certain' + reference | Case in queue |
| Draft fails validation | One revision with the exact issues; then human review | Corrected answer or holding reply | Draft and issues in the packet |
| Validator / judge unreachable | Human review (fail closed) | Holding reply | Reason: judge_unavailable |
| Graph exception or 60 s timeout | Safe fallback: apology + review row | Apology + reference | Case with error text |
| Safety model unreachable | Continue without it (extra layer only) | Normal flow | — |
| Slack unavailable | Alert dropped; the queue still holds the case | Unchanged | Case visible in the UI / API |
| Duplicate delivery / retry | Idempotent writes; Slack event de-duplication | Unchanged | — |
| Database busy | Transient lock retried; atomic id allocation | Unchanged | — |

## 10. Known gaps

- There is no payment processor or order system behind the tools: a refund is a record in a synthetic database, not a payout.
- Pending reviews have no SLA timer, so nobody is alerted if staff never respond.
- No tool changes a payment method or plan; those are guidance-only.
- Escalation holding replies are English only.
- The rate limiter and answer cache are per process; SQLite is single-writer (Postgres/Redis for scale).
- The judge and the specialists are the same model family, so shared blind spots are possible.
- This scope was written after the system was built; it documents what exists and where it falls short.
