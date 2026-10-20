"""The ``<context>`` JSON block every prompt carries. The OfflineResponder reads only this block."""

import json
import re
from typing import Any

from ..models.base import canonical_json

_BLOCK = re.compile(r"<context>\s*(.*?)\s*</context>", re.DOTALL)


def render_context(data: dict[str, Any]) -> str:
    return f"<context>\n{canonical_json(data)}\n</context>"


def extract_context(text: str) -> dict[str, Any]:
    """The JSON object in the first ``<context>`` block of ``text`` (empty if there is none)."""
    match = _BLOCK.search(text)
    if match is None:
        return {}
    value = json.loads(match.group(1))
    return value if isinstance(value, dict) else {}


def estimate_tokens(text: str) -> int:
    """Synthetic token count used by the Fake_LLM: about four characters per token."""
    return max(1, -(-len(text) // 4))
