"""Graph topology.

 START → intake ──(blocked)──────────────────────────────► finalize_rejected → END
           │ (ok)
           ├──► dispatcher ─┐
           ├──► safety_check├─(join)─► triage ─(off_topic)─► off_topic_reply → END
           └──► kb_warm ────┘            │
                                         │ Send() fan-out, parallel
                              ┌──────────┼───────────────┐
                              ▼          ▼               ▼
                          specialist  specialist   escalation_node        (billing/technical/general, 0-2 + escalation)
                              └──────────┴───────┬───────┘
                                                 ▼
                                               merge ──► validator ─┬─ approve ─► deliver ─► END
                                                  ▲                 ├─ revise (≤2) ─► revise ─┐ (Send fan-out again)
                                                  └─────────────────┼─────────────────────────┘
                                                                    └─ human_review ─► human_prepare ─► human_wait (interrupt) ─► human_resolve ─► END
"""
from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from app.graph import nodes as N
from app.graph.state import SupportState


def build_graph(checkpointer=None):
    g = StateGraph(SupportState)
    g.add_node("intake", N.intake)
    g.add_node("finalize_rejected", N.finalize_rejected)
    g.add_node("serve_cache", N.serve_cache)
    g.add_node("dispatcher", N.dispatcher_node)
    g.add_node("safety_check", N.safety_check)
    g.add_node("kb_warm", N.kb_warm)
    g.add_node("triage", N.triage)
    g.add_node("off_topic_reply", N.off_topic_reply)
    g.add_node("specialist", N.specialist_node)
    g.add_node("escalation_node", N.escalation_node)
    g.add_node("merge", N.merge)
    g.add_node("validator", N.validator_node)
    g.add_node("revise", N.revise)
    g.add_node("deliver", N.deliver)
    g.add_node("human_prepare", N.human_prepare)
    g.add_node("human_wait", N.human_wait)
    g.add_node("human_resolve", N.human_resolve)

    g.add_edge(START, "intake")
    g.add_conditional_edges("intake", N.after_intake, ["finalize_rejected", "serve_cache", "dispatcher", "safety_check", "kb_warm"])
    g.add_edge(["dispatcher", "safety_check", "kb_warm"], "triage")
    g.add_conditional_edges("triage", N.route_after_triage, ["off_topic_reply", "specialist", "escalation_node"])
    g.add_edge("specialist", "merge")
    g.add_edge("escalation_node", "merge")
    g.add_edge("merge", "validator")
    g.add_conditional_edges("validator", N.route_after_validation, ["deliver", "revise", "human_prepare"])
    g.add_conditional_edges("revise", N.route_revise, ["specialist"])
    g.add_edge("human_prepare", "human_wait")
    g.add_edge("human_wait", "human_resolve")
    for terminal in ("finalize_rejected", "serve_cache", "off_topic_reply", "deliver", "human_resolve"):
        g.add_edge(terminal, END)
    return g.compile(checkpointer=checkpointer or MemorySaver())
