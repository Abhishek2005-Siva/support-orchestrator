from __future__ import annotations

from typing import Annotated, TypedDict


def reduce_outputs(old: list | None, new: list | None) -> list:
    """Reducer for parallel specialist outputs. A leading "RESET" marker clears the list (used by the revise loop)."""
    new = new or []
    if new and new[0] == "RESET":
        return list(new[1:])
    return list(old or []) + list(new)


class SupportState(TypedDict, total=False):
    # request
    query_id: str
    customer_id: str
    channel: str
    message: str            # raw customer text
    history: list[dict]
    clean_message: str      # sanitised, secrets masked: the only text agents see
    # intake
    input: dict             # InputVerdict summary
    profile: dict
    open_tickets: int
    safety: dict
    cache_key: str
    cache_hit: dict
    # triage
    dispatch: dict
    # specialists (parallel, merged by reducer)
    specialist_outputs: Annotated[list[dict], reduce_outputs]
    escalation_output: dict
    merged: dict            # {"reply","evidence","sources","confidence","needs_human","requires_human_approval","is_template"}
    # validation loop
    validation: dict
    retry_count: int
    feedback: list[str]
    prior_actions: list[dict]   # evidence of write actions (and their verifications) done by an earlier attempt: carried into the revision
    # human review
    review_id: str
    holding_reply: str
    human_decision: dict
    # result
    final_reply: str
    status: str             # delivered | human_review | rejected | error
    flags: dict
    timings: dict
