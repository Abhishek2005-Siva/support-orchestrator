"""Prometheus metrics (/metrics)."""
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram, generate_latest

registry = CollectorRegistry()
REQS = Counter("support_requests_total", "HTTP requests", ["route", "status"], registry=registry)
QUERIES = Counter("support_queries_total", "Support queries by outcome", ["status", "channel"], registry=registry)
LATENCY = Histogram("support_query_latency_seconds", "End-to-end query latency", buckets=(.05, .1, .25, .5, 1, 2, 3, 5, 8, 13, 21, 34, 60), registry=registry)
RATE_LIMITED = Counter("support_rate_limited_total", "429 responses", ["scope"], registry=registry)
GUARD = Counter("support_guardrail_events_total", "Guardrail interventions", ["kind"], registry=registry)
INFLIGHT = Gauge("support_inflight_queries", "Queries currently running", registry=registry)
REVIEW_PENDING = Gauge("support_review_queue_pending", "Pending human review items", registry=registry)
LLM_CALLS = Gauge("support_llm_calls", "LLM gateway counters", ["stat"], registry=registry)
CACHE = Gauge("support_answer_cache", "Answer cache stats", ["stat"], registry=registry)


def render() -> bytes:
    return generate_latest(registry)
