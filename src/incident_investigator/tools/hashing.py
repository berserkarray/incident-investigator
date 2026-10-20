"""Hash-derived uniform numbers shared by the Failure_Injector and the Retry_Policy (Req 14.2, 15.2)."""

import hashlib

from ..models.base import canonical_json
from ..models.tools import ToolInputBase


def unit_interval(text: str) -> float:
    """A uniform number in ``[0, 1)`` from the first 8 bytes of ``sha256(text)``."""
    value = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
    return min(value / 2**64, 1.0 - 2**-53)  # the division can round up to 1.0 for the largest values


def input_key(inp: ToolInputBase) -> str:
    """Canonical JSON of a validated Tool input: equal field values give equal keys."""
    return canonical_json(inp)
