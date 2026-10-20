"""Retry_Policy: exponential backoff with hash-derived jitter (Req 15.1, 15.2).

The jitter depends only on the Run Seed, the Tool, the canonical Tool input and the retry number,
so equal combinations wait equally long in every Run. Waiting itself happens on the Clock.
"""

from ..models.config import RetrySettings
from ..models.tools import ToolInputBase
from ..models.vocab import ToolName
from .hashing import input_key, unit_interval


class RetryPolicy:
    def __init__(self, seed: int, max_attempts: int, base_s: float, max_s: float):
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if not 0 < base_s <= max_s:
            raise ValueError("delays must satisfy 0 < base_s <= max_s")
        self.seed = seed
        self.max_attempts = max_attempts
        self.base_s = base_s
        self.max_s = max_s

    @classmethod
    def from_settings(cls, seed: int, settings: RetrySettings) -> "RetryPolicy":
        return cls(seed, settings.max_attempts, settings.base_delay_s, settings.max_delay_s)

    def jitter_fraction(self, tool: ToolName, inp: ToolInputBase, retry_no: int) -> float:
        return unit_interval(f"{self.seed}|{ToolName(tool)}|{input_key(inp)}|{retry_no}|jitter")

    def delay(self, tool: ToolName, inp: ToolInputBase, retry_no: int) -> float:
        """Wait before retry ``retry_no`` (1-based): ``min(cap, b + b*u)`` with ``b = base * 2**(retry_no-1)``."""
        if retry_no < 1:
            raise ValueError("retry numbers start at 1")
        b = self.base_s * 2 ** (retry_no - 1)
        return min(self.max_s, b + b * self.jitter_fraction(tool, inp, retry_no))
