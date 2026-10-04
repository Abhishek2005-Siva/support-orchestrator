"""Pydantic contracts shared across agents, graph and API."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Intent = Literal["billing", "technical", "escalation", "general", "off_topic"]
Urgency = Literal["low", "medium", "high", "critical"]
Sentiment = Literal["positive", "neutral", "negative", "angry"]
Channel = Literal["api", "slack", "web"]


class DispatchDecision(BaseModel):
    intents: list[Intent] = Field(min_length=1, max_length=3)
    urgency: Urgency = "medium"
    sentiment: Sentiment = "neutral"
    confidence: float = Field(0.8, ge=0, le=1)
    reasoning: str = Field("", max_length=400)
    forced_escalation_reasons: list[str] = []  # filled by deterministic rules, not the LLM
    refund_requested: bool = False  # the customer is asking us to refund / give money back (a request to ACT, not a question about policy)
    sub_questions: dict[str, str] = {}  # multi-intent only: the customer's question for each specialist, in its own words

    @field_validator("sub_questions", mode="before")
    @classmethod
    def _clean_sub(cls, v):
        if not isinstance(v, dict):
            return {}
        return {k: str(x)[:250] for k, x in v.items() if k in ("billing", "technical", "general") and isinstance(x, str) and x.strip()}

    @field_validator("intents")
    @classmethod
    def _dedupe(cls, v):
        return list(dict.fromkeys(v))


class SpecialistResponse(BaseModel):
    agent: str = ""
    reply: str = Field(min_length=1, max_length=2500)
    sources: list[str] = []  # evidence ids (derived from tool evidence, not trusted from the model)
    actions_taken: list[str] = []
    confidence: float = Field(0.7, ge=0, le=1)
    needs_human: bool = False
    needs_human_reason: str | None = None
    requires_human_approval: bool = False  # e.g. refund above auto-limit
    evidence: list[dict] = []  # compact tool results for the validator's grounding checks
    iterations: int = 0
    tool_calls: int = 0


class ModelSpecialistOutput(BaseModel):
    """What the LLM is asked to emit (we then enrich it into SpecialistResponse)."""
    model_config = ConfigDict(extra="ignore")
    reply: str = Field(min_length=1, max_length=2500)
    confidence: float = Field(0.7, ge=0, le=1)
    needs_human: bool = False
    needs_human_reason: str | None = None
    actions_taken: list[str] = []


class ValidationIssue(BaseModel):
    code: str
    severity: Literal["info", "warning", "critical"] = "warning"
    detail: str = ""


class ValidationResult(BaseModel):
    verdict: Literal["approve", "revise", "human_review"]
    issues: list[ValidationIssue] = []
    confidence: float = Field(0.8, ge=0, le=1)
    layer: str = ""  # which layer decided: deterministic | llm | both


class QueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    message: str = Field(min_length=1, max_length=2000)
    channel: Channel = "api"
    history: list[dict[str, str]] = Field(default_factory=list, max_length=10)
    wait: bool = True


class QueryResponse(BaseModel):
    query_id: str
    status: Literal["processing", "delivered", "human_review", "rejected", "error"]
    reply: str | None = None
    intents: list[str] = []
    review_id: str | None = None
    latency_ms: int | None = None
    flags: dict[str, Any] = {}
