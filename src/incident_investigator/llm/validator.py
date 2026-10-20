"""Structured_Output_Validator: parse, validate, one repair, then an Agent_Failure (Req 5.4-5.6)."""

import json
import re
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from .base import SemanticCheck, ValidationContext

M = TypeVar("M", bound=BaseModel)

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class StructuredOutputFailure(Exception):
    """The repaired output still failed validation. This is an Agent_Failure of the calling Agent."""

    def __init__(self, errors: list[str]):
        super().__init__("structured output failed validation after one repair: " + "; ".join(errors[:5]))
        self.errors = errors


class OutputInvalid(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def extract_json(text: str) -> Any:
    """The first fenced JSON block, or else the first top-level JSON object in ``text``."""
    if not text or not text.strip():
        raise OutputInvalid(["<root>: empty output"])
    candidates = [m.group(1) for m in _FENCE.finditer(text)] + [text]
    decoder = json.JSONDecoder()
    for candidate in candidates:
        start = candidate.find("{")
        while start != -1:
            try:
                value, _ = decoder.raw_decode(candidate, start)
                return value
            except json.JSONDecodeError:
                start = candidate.find("{", start + 1)
    raise OutputInvalid(["<root>: output is not a JSON object"])


def parse_output(text: str, model: type[M], ctx: ValidationContext, check: SemanticCheck | None = None) -> M:
    """Validate ``text`` against ``model`` and the semantic ``check``; raise ``OutputInvalid`` listing ``loc: msg``."""
    data = extract_json(text)
    try:
        value = model.model_validate(data)
    except ValidationError as exc:
        raise OutputInvalid(
            [f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors(include_url=False)]
        ) from None
    if check is not None:
        problems = check(value, ctx)
        if problems:
            raise OutputInvalid(problems)
    return value


def repair_prompt(user: str, previous: str, errors: list[str]) -> str:
    listed = "\n".join(f"- {e}" for e in errors)
    return (
        f"{user}\n\nYour previous answer failed validation:\n{listed}\n\n"
        f"Previous answer:\n{previous}\n\nReturn only the corrected JSON object."
    )
