"""Relational schema for the simulated bank + system tables.

Bank tables:   customers, accounts, cards, merchants, transactions (the ledger), transfers, disputes, fraud_alerts,
               fee_waivers, tickets, service_components
Knowledge:     policies, kg_nodes, kg_edges, verifications
System tables: human_review_queue, audit_log, security_events, conversations, api_credentials, meta, id_sequences
IDs are human-readable and regex-validated at the tool boundary (CUST-######, TXN-########, CARD-#######, ...).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Meta(Base):
    __tablename__ = "meta"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class IdSequence(Base):
    """Atomic counters for human-readable ids (REF-, TCK-, HRQ-). `UPDATE ... RETURNING` takes the write lock, so concurrent
    requests can never be issued the same id (count(*)+1 races under load)."""
    __tablename__ = "id_sequences"
    name: Mapped[str] = mapped_column(String(8), primary_key=True)
    value: Mapped[int] = mapped_column(Integer, default=0)


class Customer(Base):
    __tablename__ = "customers"
    id: Mapped[str] = mapped_column(String(11), primary_key=True)  # CUST-000001
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(200), unique=True)
    phone: Mapped[str] = mapped_column(String(40))
    segment: Mapped[str] = mapped_column(String(16))  # standard | premium | private
    kyc_status: Mapped[str] = mapped_column(String(16))  # verified | pending | failed
    risk_flag: Mapped[str] = mapped_column(String(16), default="none")  # none | aml_review  (INTERNAL: never shown to customers)
    country: Mapped[str] = mapped_column(String(2))
    created_at: Mapped[datetime] = mapped_column(DateTime)


class Account(Base):
    __tablename__ = "accounts"
    id: Mapped[str] = mapped_column(String(12), primary_key=True)  # ACC-00000001
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    type: Mapped[str] = mapped_column(String(12))  # checking | savings | credit_card
    number_last4: Mapped[str] = mapped_column(String(4))
    balance_cents: Mapped[int] = mapped_column(Integer)
    overdraft_limit_cents: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(10))  # active | frozen | closed
    opened_at: Mapped[datetime] = mapped_column(DateTime)


class Card(Base):
    __tablename__ = "cards"
    id: Mapped[str] = mapped_column(String(12), primary_key=True)  # CARD-0000001
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    type: Mapped[str] = mapped_column(String(8))  # debit | credit | virtual
    network: Mapped[str] = mapped_column(String(12))  # visa | mastercard
    last4: Mapped[str] = mapped_column(String(4))
    status: Mapped[str] = mapped_column(String(18))  # active | blocked | lost | expired | pending_activation
    expiry: Mapped[str] = mapped_column(String(5))  # MM/YY
    daily_limit_cents: Mapped[int] = mapped_column(Integer)
    contactless: Mapped[int] = mapped_column(Integer, default=1)
    intl_enabled: Mapped[int] = mapped_column(Integer, default=0)
    issued_at: Mapped[datetime] = mapped_column(DateTime)


class Merchant(Base):
    __tablename__ = "merchants"
    id: Mapped[str] = mapped_column(String(10), primary_key=True)  # MER-000001
    name: Mapped[str] = mapped_column(String(80))
    mcc: Mapped[str] = mapped_column(String(4))
    category: Mapped[str] = mapped_column(String(40))
    country: Mapped[str] = mapped_column(String(2))
    risk: Mapped[str] = mapped_column(String(6))  # low | medium | high


class Transaction(Base):
    """The ledger: every movement on every account (purchases, ATM, transfers in/out, fees, refunds, provisional credits)."""
    __tablename__ = "transactions"
    id: Mapped[str] = mapped_column(String(12), primary_key=True)  # TXN-00000001
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"), index=True)
    card_id: Mapped[str | None] = mapped_column(ForeignKey("cards.id"))
    kind: Mapped[str] = mapped_column(String(18))  # card_purchase | atm_withdrawal | transfer_out | transfer_in | fee | refund | interest | provisional_credit | payroll
    direction: Mapped[str] = mapped_column(String(6))  # debit | credit
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    merchant_id: Mapped[str | None] = mapped_column(ForeignKey("merchants.id"))
    description: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(10))  # pending | posted | reversed | declined
    decline_reason: Mapped[str | None] = mapped_column(String(30))
    channel: Mapped[str] = mapped_column(String(8))  # pos | online | atm | app | branch
    country: Mapped[str] = mapped_column(String(2))
    linked_txn_id: Mapped[str | None] = mapped_column(String(12))
    created_at: Mapped[datetime] = mapped_column(DateTime)  # authorisation time
    posted_at: Mapped[datetime | None] = mapped_column(DateTime)


class Transfer(Base):
    __tablename__ = "transfers"
    id: Mapped[str] = mapped_column(String(12), primary_key=True)  # TRF-00000001
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    from_account_id: Mapped[str] = mapped_column(ForeignKey("accounts.id"))
    to_name: Mapped[str] = mapped_column(String(80))
    to_account_masked: Mapped[str] = mapped_column(String(20))
    rail: Mapped[str] = mapped_column(String(8))  # internal | ach | wire | sepa
    amount_cents: Mapped[int] = mapped_column(Integer)
    fee_cents: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(12))  # pending | submitted | completed | returned | failed | cancelled
    return_code: Mapped[str | None] = mapped_column(String(4))  # ACH return reason, e.g. R01
    reference: Mapped[str] = mapped_column(String(40))
    txn_id: Mapped[str | None] = mapped_column(String(12))
    initiated_at: Mapped[datetime] = mapped_column(DateTime)
    expected_by: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class Dispute(Base):
    __tablename__ = "disputes"
    id: Mapped[str] = mapped_column(String(11), primary_key=True)  # DSP-000001
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    txn_id: Mapped[str] = mapped_column(ForeignKey("transactions.id"), index=True)
    reason: Mapped[str] = mapped_column(String(20))  # duplicate | unauthorized | not_received | wrong_amount
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24))  # provisional_credit_issued | pending_approval | under_review | won | lost | rejected
    provisional_txn_id: Mapped[str | None] = mapped_column(String(12))
    verification_id: Mapped[str | None] = mapped_column(String(11))
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FraudAlert(Base):
    """Simulated fraud-engine output (read-only for agents)."""
    __tablename__ = "fraud_alerts"
    id: Mapped[str] = mapped_column(String(11), primary_key=True)  # FRD-000001
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    txn_id: Mapped[str] = mapped_column(String(12))
    signal: Mapped[str] = mapped_column(String(30))  # foreign_country | velocity | high_risk_mcc | new_device
    score: Mapped[int] = mapped_column(Integer)  # INTERNAL: never shown to customers
    status: Mapped[str] = mapped_column(String(12), default="open")  # open | confirmed | false_positive
    created_at: Mapped[datetime] = mapped_column(DateTime)


class FeeWaiver(Base):
    __tablename__ = "fee_waivers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    fee_txn_id: Mapped[str] = mapped_column(String(12))
    waived_at: Mapped[datetime] = mapped_column(DateTime)


class Policy(Base):
    """Bank policy as data. Each row is linked to the regulation it implements and the help article that explains it."""
    __tablename__ = "policies"
    id: Mapped[str] = mapped_column(String(14), primary_key=True)  # POL-DUP-01
    title: Mapped[str] = mapped_column(String(120))
    rule: Mapped[str] = mapped_column(Text)
    params: Mapped[str] = mapped_column(Text)  # JSON
    regulation: Mapped[str] = mapped_column(String(60))
    kb_article: Mapped[str | None] = mapped_column(String(60))
    version: Mapped[str] = mapped_column(String(8), default="1.0")
    owner: Mapped[str] = mapped_column(String(40), default="Operations")


class KgNode(Base):
    """Knowledge layer of the knowledge graph (issue types, checks, policies, regulations, actions, KB articles).
    The operational layer (customer, accounts, transactions...) is derived live from the tables."""
    __tablename__ = "kg_nodes"
    id: Mapped[str] = mapped_column(String(60), primary_key=True)  # e.g. issue:duplicate_charge
    type: Mapped[str] = mapped_column(String(16), index=True)  # issue | check | policy | regulation | action | kb
    label: Mapped[str] = mapped_column(String(120))
    props: Mapped[str] = mapped_column(Text, default="{}")


class KgEdge(Base):
    __tablename__ = "kg_edges"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    src: Mapped[str] = mapped_column(String(60), index=True)
    dst: Mapped[str] = mapped_column(String(60), index=True)
    rel: Mapped[str] = mapped_column(String(24))


class Verification(Base):
    """Record of one investigation: what was read (tables, policies, KG paths, KB), what was found, what was decided."""
    __tablename__ = "verifications"
    id: Mapped[str] = mapped_column(String(11), primary_key=True)  # VER-000001
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    query_id: Mapped[str | None] = mapped_column(String(40), index=True)
    issue_type: Mapped[str] = mapped_column(String(24))
    subject_id: Mapped[str] = mapped_column(String(14))  # txn / transfer / card id the verdict is about
    decision: Mapped[str] = mapped_column(String(10))  # act | wait | deny | no_action | human
    action: Mapped[str | None] = mapped_column(String(30))
    amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    report: Mapped[str] = mapped_column(Text)  # JSON: checks, consulted sources
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[str] = mapped_column(String(11), primary_key=True)  # TCK-000001
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    summary: Mapped[str] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(20))  # payments | cards | account | other
    severity: Mapped[str] = mapped_column(String(10))  # low | medium | high | critical
    status: Mapped[str] = mapped_column(String(12))  # open | resolved | escalated
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ServiceComponent(Base):
    __tablename__ = "service_components"
    name: Mapped[str] = mapped_column(String(40), primary_key=True)
    status: Mapped[str] = mapped_column(String(16))  # operational | degraded | outage | maintenance
    note: Mapped[str] = mapped_column(String(300), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class HumanReview(Base):
    __tablename__ = "human_review_queue"
    id: Mapped[str] = mapped_column(String(12), primary_key=True)  # HRQ-000001
    query_id: Mapped[str] = mapped_column(String(40), index=True)
    customer_id: Mapped[str] = mapped_column(String(11), index=True)
    priority: Mapped[str] = mapped_column(String(10))
    reason: Mapped[str] = mapped_column(String(300))
    summary: Mapped[str] = mapped_column(Text)
    customer_message: Mapped[str] = mapped_column(Text)
    draft_reply: Mapped[str | None] = mapped_column(Text)
    holding_reply: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending | approved | edited | rejected
    reviewer: Mapped[str | None] = mapped_column(String(80))
    resolution_note: Mapped[str | None] = mapped_column(Text)
    final_reply: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    query_id: Mapped[str | None] = mapped_column(String(40), index=True)
    actor: Mapped[str] = mapped_column(String(40))  # agent name or user
    action: Mapped[str] = mapped_column(String(60))
    customer_id: Mapped[str | None] = mapped_column(String(11))
    args: Mapped[str | None] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(20))  # ok | blocked | error | pending_approval


class SecurityEvent(Base):
    __tablename__ = "security_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    kind: Mapped[str] = mapped_column(String(40), index=True)  # sqli_attempt | prompt_injection | authz_violation | ...
    query_id: Mapped[str | None] = mapped_column(String(40))
    customer_id: Mapped[str | None] = mapped_column(String(11))
    detail: Mapped[str] = mapped_column(Text)
    blocked: Mapped[int] = mapped_column(Integer, default=1)


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(40), primary_key=True)  # query_id
    customer_id: Mapped[str] = mapped_column(String(11), index=True)
    channel: Mapped[str] = mapped_column(String(10))
    message: Mapped[str] = mapped_column(Text)
    channel_ref: Mapped[str | None] = mapped_column(String(80))  # e.g. slack "C123:1699999999.000100" (channel:thread_ts) for follow-ups
    status: Mapped[str] = mapped_column(String(16), default="processing")  # processing|delivered|human_review|rejected|error
    final_reply: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class ApiCredential(Base):
    __tablename__ = "api_credentials"
    client_id: Mapped[str] = mapped_column(String(40), primary_key=True)  # customer id or staff handle
    secret_hash: Mapped[str] = mapped_column(String(200))
    role: Mapped[str] = mapped_column(String(16))  # customer | agent_staff | admin
    customer_id: Mapped[str | None] = mapped_column(String(11))
    slack_user_id: Mapped[str | None] = mapped_column(String(20), index=True)


Index("ix_txn_cust_created", Transaction.customer_id, Transaction.created_at)
Index("ix_txn_acct_created", Transaction.account_id, Transaction.created_at)
