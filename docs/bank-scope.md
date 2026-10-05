# Orbit Bank: what the system is responsible for

A retail bank's support and operations desk, run by AI agents on **simulated bank infrastructure**. The agents do not just answer: they *investigate* (ledger, cards, transfers, fraud signals), *verify* against policy and regulation, and then *act* (block a card, file a dispute, issue provisional credit, reverse a fee) or hand the case to a human.

All data is synthetic. Public data used: PolyAI **Banking77** (GitHub, 13,102 real banking customer queries in 77 intents) for routing evaluation and phrasing.

## 1. Principle: verify, then act

Every state-changing action needs a **verification record** first.

1. `verify_transaction_issue` collects evidence from the **databases** (accounts, cards, transactions, transfers, fraud alerts), looks up the governing **policies** (policy table) and **regulations**, asks the **knowledge graph** which checks and policies apply to this kind of issue, and cites the **knowledge-base** article.
2. It returns findings, a decision (`act` / `deny` / `wait` / `human`) and a `verification_id` stored in the database.
3. Action tools (`file_dispute`, `reverse_fee`, `cancel_transfer`, `request_replacement_card`) **refuse** without a matching verification, and re-derive the decision themselves. Blocking a card is the one protective action allowed without it.

The model never decides money, only phrases the outcome.

## 2. Tasks the system must handle

| id | task | example | handled by | autonomy |
|---|---|---|---|---|
| T1 | Duplicate / double charge | "I was charged twice at Shell" | payments | verify, then dispute with provisional credit up to the limit; above it, human approval. A pending hold plus a posted charge is explained, not disputed |
| T2 | Unrecognised card payment | "I don't recognise this $212 payment" | cards + payments | verify (location, channel, velocity, history); block card if signals; dispute as unauthorised |
| T3 | Lost or stolen card | "I lost my wallet" | cards | block immediately (protective); offer replacement; list later transactions |
| T4 | Transfer not arrived / pending / returned | "My transfer hasn't arrived" | payments | trace, explain with rail timing; cancel only if still cancellable |
| T5 | Declined payment | "My card was declined at the shop" | cards | explain the decline reason; change a card control only if asked |
| T6 | Fee reversal | "Why was I charged an overdraft fee?" | payments | verify; waive once per 12 months up to the limit; else human |
| T7 | Product and how-to questions | "How do I change my PIN?" | general | knowledge base only |
| T8 | Sensitive cases | complaint, legal threat, bereavement, "closed my account", suspected laundering | escalation | human only; never auto-resolved |

Out of scope (stated in the UI): real payment rails, loans and mortgages decisions, investment advice, identity verification documents.

## 3. Workflows (each step is a span in the live trace)

**T1 Duplicate charge**: guard → dispatcher (payments) → `get_transactions` → `verify_transaction_issue(duplicate_charge)` → [KG: issue → checks + policies] → checks: same card, merchant, amount within 24 h; pending vs posted; already reversed or disputed → decision → `file_dispute` (provisional credit if allowed) → validator → reply.

**T2 Unrecognised payment**: … `verify_transaction_issue(unrecognised_payment)` → checks: card present vs online, country vs home, velocity (3+ in 1 h), merchant risk, prior payments to the same merchant, open fraud alert → if fraud signals: `block_card` → `file_dispute(unauthorized)` → `request_replacement_card`.

**T3 Lost card**: `get_cards` → `block_card` → `get_transactions` since the reported time → offer replacement and dispute for any later charge.

**T4 Transfer trace**: `get_transfer_status` → `verify_transaction_issue(transfer_problem)` → checks: rail timing against the policy table, cut-off, return code meaning, beneficiary → explain / `cancel_transfer` / human.

**T6 Fee**: `verify_transaction_issue(fee_dispute)` → checks: fee type, prior waivers in 12 months, account standing, amount ≤ limit → `reverse_fee`.

## 4. Tools

| tool | kind | agents | notes |
|---|---|---|---|
| `search_knowledge_base` | read | all | hybrid retrieval over help articles |
| `get_customer_profile` | read | all | segment, KYC status (never internal risk flags) |
| `get_accounts` | read | payments, cards | balances, status, limits |
| `get_transactions` | read | payments, cards | filters: days, kind, status, merchant, amount |
| `get_transaction_detail` | read | payments, cards | one transaction with merchant, channel, country |
| `get_transfer_status` | read | payments | rail, expected date, return code |
| `get_cards` | read | cards | status, limits, controls |
| `get_fraud_alerts` | read | cards | simulated fraud-engine alerts |
| `get_policy` | read | payments, cards | policy rows from the database (value, regulation, KB link) |
| `query_knowledge_graph` | read | payments, cards | applicable checks, policies, regulations, actions for an issue |
| `verify_transaction_issue` | read + record | payments, cards | the investigator; stores a verification |
| `file_dispute` | **write** | payments, cards | needs verification; provisional credit by policy |
| `reverse_fee` | **write** | payments | needs verification |
| `cancel_transfer` | **write** | payments | needs verification; only pending transfers |
| `block_card` | **write, protective** | cards | allowed on lost / stolen / fraud intent |
| `request_replacement_card` | **write** | cards | customer must ask |
| `create_ticket` | write | payments, cards | only when the customer asks for a follow-up |
| `get_ticket_history`, `assign_to_human` | read, write | escalation | queue and priority |

Cross-cutting guardrails apply to every tool (identity injected from the session, allow-list per agent, intent-gated writes, parameterised SQL, per-tool caps, audit log).

## 5. Agent boundaries

| agent | owns | may not |
|---|---|---|
| Dispatcher | intent, urgency, sentiment, `action_requested` | call tools, decide anything |
| Payments | transactions, transfers, disputes, fees | block cards, touch card controls |
| Cards & fraud | cards, declines, fraud, limits, lost/stolen | move money, file fee reversals |
| General | products, rates, how-tos | read account data |
| Escalation | hand-off note, priority, ETA | answer the customer (template reply) |
| Validator | checks every figure and claim against evidence | change the reply |

## 6. Policies (database table `policies`, each linked to a regulation and a help article)

| id | rule | value | based on |
|---|---|---|---|
| POL-DUP-01 | duplicate charge: same card + merchant + amount | within 24 h | card network rules |
| POL-HOLD-01 | pending authorisation hold lasts | up to 5 business days | card network rules |
| POL-DSP-01 | dispute window from transaction date | 60 days | Reg E (EFTA) |
| POL-DSP-02 | provisional credit, auto-approved up to | $500, verified customers | Reg E §1005.11, bank policy |
| POL-DSP-03 | provisional credit above the limit | human approval | bank policy |
| POL-FRD-01 | fraud signals: 3+ transactions in 1 h, foreign country, card-not-present at a high-risk merchant category | block card | bank fraud policy |
| POL-LIA-01 | liability for card reported lost before use | $0 | Reg E §1005.6 |
| POL-FEE-01 | fee courtesy waiver | once per 12 months, up to $35 | bank policy, UDAAP |
| POL-ACH-01 | ACH transfer timing | 1-3 business days | NACHA |
| POL-WIR-01 | wire cut-off / finality | same day before 17:00 ET; irrevocable once sent | Fedwire |
| POL-CAN-01 | transfer cancellation | only while status is pending and not yet submitted | bank policy |
| POL-LIM-01 | daily limits | debit card $2,500 spend, $800 ATM; wire $25,000 | bank policy |
| POL-KYC-01 | review triggers | cash/wire ≥ $10,000, structuring pattern | BSA / AML |
| POL-AML-01 | suspected laundering | escalate to compliance; **never tip off the customer** | BSA / AML |

## 7. Knowledge graph

Two layers.

- **Knowledge layer** (materialised in `kg_nodes` / `kg_edges`): `IssueType` -REQUIRES_CHECK-> `Check`; `IssueType` -GOVERNED_BY-> `Policy`; `Policy` -BASED_ON-> `Regulation`; `Policy` -DOCUMENTED_IN-> `KBArticle`; `IssueType` -RESOLVED_BY-> `Action`; `Action` -GATED_BY-> `Policy`. The verifier reads this to decide which checks to run.
- **Operational layer** (derived live from the tables, so it can never be stale): `Customer` -OWNS-> `Account` / `Card`; `Account` -HAS_TXN-> `Transaction` -AT-> `Merchant` -IN_CATEGORY-> `MCC`; `Transaction` -DISPUTED_BY-> `Dispute`; `Transfer` -FROM-> `Account`.

## 8. Trust boundaries and security

Customer text is untrusted → input guard (injection, SQL, secrets masked, other customers' ids) → agents (untrusted by construction) → tool runtime (identity injected, allow-list, intent gates, verification required, caps) → parameterised, customer-scoped queries. Internal fields (`risk_flag`, AML status, fraud scores) never appear in customer-facing tool output. Staff act through role-checked endpoints and Slack signature verification.

## 9. Failure and escalation behaviour

Validator unsure or unreachable → human review (fail closed). Verification says `human` (amount above limit, conflicting evidence, KYC pending, AML flag) → approval review; the customer gets a truthful holding message and never hears that money moved before it did. Model outage → fallback chain, then holding reply.

## 10. How it is measured

A golden set and a separate hold-out set from the simulated bank (ground truth in `data/seed_manifest.json`: transaction ids, amounts, expected decision, expected side effects), plus Banking77 routing accuracy, plus the adversarial corpora. Targets: accuracy, 0 unsafe actions, p50 latency.
