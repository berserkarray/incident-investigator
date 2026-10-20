"""Failure_Injector: deterministic timeouts, partial results and stale data (Req 14, design D1).

Every outcome is a pure function of ``(seed, incident ID, Tool, canonical input, attempt)``, so it does
not depend on the caller, the order or concurrency of calls, or the time (Req 14.2).
"""

import hashlib
import math
from dataclasses import dataclass
from datetime import timedelta

from ..models.config import FailureInjectionSettings
from ..models.snapshot import IncidentSnapshot
from ..models.tools import ToolError, ToolInputBase, ToolRecord, ToolResult, input_time_range, record_timestamp
from ..models.vocab import NO_STALE_TOOLS, ErrorCode, FailureType, ToolName
from .hashing import input_key, unit_interval
from .impl import cut_to_limit


@dataclass(frozen=True)
class InjectedOutcome:
    """The failure chosen for one Tool_Call; ``failure`` is None for no injected failure."""

    tool: ToolName
    attempt: int
    key: str
    failure: FailureType | None
    forced: bool = False


class FailureInjector:
    def __init__(self, seed: int, snapshot: IncidentSnapshot, settings: FailureInjectionSettings):
        self.seed = seed
        self.incident_id = snapshot.incident_id
        self.profile = snapshot.failure_profile
        self.settings = settings

    def key(self, tool: ToolName, inp: ToolInputBase, attempt: int) -> str:
        return f"{self.seed}|{self.incident_id}|{tool}|{input_key(inp)}|{attempt}"

    def _u(self, key: str, purpose: str) -> float:
        return unit_interval(f"{key}|{purpose}")

    def probabilities(self, tool: ToolName) -> dict[FailureType, float]:
        """Per-type probabilities for ``tool``; stale-data is 0 for Tools without a time range (Req 14.1)."""
        s = self.settings
        return {
            FailureType.TIMEOUT: s.timeout_p,
            FailureType.PARTIAL: s.partial_p,
            FailureType.STALE: 0.0 if tool in NO_STALE_TOOLS else s.stale_p,
        }

    def outcome(self, tool: ToolName, inp: ToolInputBase, attempt: int) -> InjectedOutcome:
        if attempt < 1:
            raise ValueError("attempt numbers start at 1")
        tool = ToolName(tool)
        key = self.key(tool, inp, attempt)
        forced = self.profile.forced_for(tool)
        if forced is not None:  # Req 14.6: every attempt, every Seed, in place of random selection
            return InjectedOutcome(tool, attempt, key, forced, forced=True)
        if not self.settings.random_enabled:  # Req 14.7
            return InjectedOutcome(tool, attempt, key, None)
        u = self._u(key, "type")
        upper = 0.0
        for failure, p in self.probabilities(tool).items():
            upper += p
            if p > 0 and u < upper:
                return InjectedOutcome(tool, attempt, key, failure)
        return InjectedOutcome(tool, attempt, key, None)

    def build_result(self, outcome: InjectedOutcome, inp: ToolInputBase, matches: list[ToolRecord]) -> ToolResult:
        """Shape the Tool_Result for ``outcome`` from every matching record, before the limit is applied."""
        tool = outcome.tool
        if outcome.failure is FailureType.TIMEOUT:  # Req 14.3
            return ToolResult(tool=tool, error=ToolError(code=ErrorCode.TIMEOUT, message=f"{tool} timed out"))
        records, truncated = cut_to_limit(matches, inp.limit)
        if outcome.failure is FailureType.PARTIAL:  # Req 14.4
            return ToolResult(tool=tool, records=self._partial(outcome.key, records), partial=True, truncated=truncated)
        if outcome.failure is FailureType.STALE:  # Req 14.5
            time_range = input_time_range(inp)
            if time_range is not None:
                span_us = (time_range.end - time_range.start) // timedelta(microseconds=1)
                offset = min(math.floor(self._u(outcome.key, "asof") * span_us), span_us - 1)
                as_of = time_range.start + timedelta(microseconds=offset)
                kept = [r for r in matches if (ts := record_timestamp(r)) is not None and ts <= as_of]
                records, truncated = cut_to_limit(kept, inp.limit)
                return ToolResult(tool=tool, records=records, stale=True, as_of=as_of, truncated=truncated)
        return ToolResult(tool=tool, records=records, truncated=truncated)

    def _partial(self, key: str, records: tuple[ToolRecord, ...]) -> tuple[ToolRecord, ...]:
        """A strict, order-preserving subset: none for n <= 1, otherwise 1..n-1 records chosen by hash."""
        n = len(records)
        if n <= 1:
            return ()
        k = min(1 + math.floor(self._u(key, "k") * (n - 1)), n - 1)
        rank = sorted(
            range(n),
            key=lambda i: (hashlib.sha256(f"{key}|{records[i].record_id}".encode("utf-8")).digest(), i),
        )
        keep = sorted(rank[:k])
        return tuple(records[i] for i in keep)

    def apply(self, tool: ToolName, inp: ToolInputBase, attempt: int, matches: list[ToolRecord]) -> ToolResult:
        """The ToolLayer port: choose the outcome for this attempt and shape the result."""
        return self.build_result(self.outcome(tool, inp, attempt), inp, matches)
