"""Relational schema for the mock company backend + system tables.

Business tables: customers, invoices, payments, refund_requests, tickets, error_logs, service_components
System tables:   human_review_queue, audit_log, security_events, conversations, api_credentials, meta
IDs are human-readable and regex-validated at the tool boundary (CUST-######, INV-########, ...).
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
    tier: Mapped[str] = mapped_column(String(16))  # standard | premium | vip
    plan: Mapped[str] = mapped_column(String(16))  # free | starter | pro | business | enterprise
    billing_cycle: Mapped[str] = mapped_column(String(8))  # monthly | annual
    account_status: Mapped[str] = mapped_column(String(16))  # active | suspended | closed
    card_last4: Mapped[str | None] = mapped_column(String(4))
    card_expiry: Mapped[str | None] = mapped_column(String(5))  # MM/YY
    country: Mapped[str] = mapped_column(String(2))
    created_at: Mapped[datetime] = mapped_column(DateTime)


class Invoice(Base):
    __tablename__ = "invoices"
    id: Mapped[str] = mapped_column(String(12), primary_key=True)  # INV-00000001
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="USD")
    description: Mapped[str] = mapped_column(String(200))
    period: Mapped[str] = mapped_column(String(7))  # YYYY-MM service period
    issued_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(12))  # paid | open | failed | refunded | void


class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[str] = mapped_column(String(12), primary_key=True)  # PAY-00000001
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"), index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12))  # succeeded | failed | refunded
    failure_reason: Mapped[str | None] = mapped_column(String(40))  # card_declined | insufficient_funds | expired_card
    created_at: Mapped[datetime] = mapped_column(DateTime)


class RefundRequest(Base):
    __tablename__ = "refund_requests"
    id: Mapped[str] = mapped_column(String(12), primary_key=True)  # REF-00000001
    invoice_id: Mapped[str] = mapped_column(ForeignKey("invoices.id"), index=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20))  # pending_approval | approved | rejected
    created_by: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Ticket(Base):
    __tablename__ = "tickets"
    id: Mapped[str] = mapped_column(String(11), primary_key=True)  # TCK-000001
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    summary: Mapped[str] = mapped_column(String(300))
    category: Mapped[str] = mapped_column(String(20))  # billing | technical | account | other
    severity: Mapped[str] = mapped_column(String(10))  # low | medium | high | critical
    status: Mapped[str] = mapped_column(String(12))  # open | resolved | escalated
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ErrorLog(Base):
    __tablename__ = "error_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime)
    level: Mapped[str] = mapped_column(String(8))
    code: Mapped[str] = mapped_column(String(40))
    message: Mapped[str] = mapped_column(String(300))


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


Index("ix_invoices_cust_issued", Invoice.customer_id, Invoice.issued_at)
