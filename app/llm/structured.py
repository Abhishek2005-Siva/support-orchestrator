"""Structured output with validation + repair loop.

Open models are less reliable than frontier ones at strict JSON, so every structured call goes through:
  1. json_object response format (provider-side JSON constraint)
  2. tolerant extraction (strips code fences / prose around the object)
  3. Pydantic validation (types, enums, ranges)
  4. on failure: ONE repair turn showing the model its own output + the exact validation error
  5. still failing -> StructuredOutputError (caller decides: safe default / human review)
Guardrail id: G-LLM-01 (schema-validated, self-repairing output).
"""
from __future__ import annotations

import json
import re
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.llm.gateway import LLMResult, get_gateway
from app.observability.tracing import tracer

T = TypeVar("T", bound=BaseModel)


class StructuredOutputError(Exception):
    pass


_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text: str) -> dict:
    """Pull the first JSON object out of arbitrary model text."""
    text = text.strip()
    m = _FENCE.search(text)
    if m:
        text = m.group(1).strip()
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    while start != -1:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            elif c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        break
        start = text.find("{", start + 1)
    raise ValueError("no JSON object found")


def parse_model(text: str, model_cls: type[T]) -> T:
    return model_cls.model_validate(extract_json(text))


async def structured_call(role: str, messages: list[dict], model_cls: type[T], *, name: str | None = None,
                          max_repairs: int = 1, **kw) -> tuple[T, LLMResult]:
    gw = get_gateway()
    msgs = list(messages)
    last_err = ""
    res: LLMResult | None = None
    for attempt in range(max_repairs + 1):
        res = await gw.chat(role, msgs, json_mode=True, name=name, **kw)
        try:
            return parse_model(res.content, model_cls), res
        except (ValueError, ValidationError) as e:
            last_err = str(e)[:400]
            tracer.event("llm.structured_repair", role=role, attempt=attempt, error=last_err)
            msgs = msgs + [
                {"role": "assistant", "content": res.content},
                {"role": "user", "content": f"Your reply was not valid for the required schema: {last_err}\n"
                                            "Reply again with ONLY a corrected JSON object."},
            ]
    raise StructuredOutputError(f"{role}: could not obtain valid {model_cls.__name__}: {last_err}")
