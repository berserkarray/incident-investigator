"""Ports between the Tool_Layer, its callers and the Run_Ledger."""

from collections.abc import Mapping
from typing import Protocol

from ..models.base import JsonValue, TimeRange
from ..models.evidence import EvidenceItem, ObservationSummary, Observation
from ..models.gaps import DataGap, DataGapDraft
from ..models.tools import ToolInput, ToolRecord
from ..models.vocab import Caller, Reliability, TerminationReason, ToolName


class ToolPort(Protocol):
    """What a Specialist_Agent (scoped to its own Tools) or the Baseline_Agent receives."""

    async def request(self, task_id: str | None, tool: ToolName, params: Mapping[str, JsonValue]) -> Observation: ...


class EvidenceSink(Protocol):
    """The Run_Ledger writer. Only the Tool_Layer appends Evidence_Items."""

    async def append_evidence(
        self,
        *,
        tool: ToolName,
        query: ToolInput,
        summary: ObservationSummary,
        time_range: TimeRange | None,
        reliability: Reliability,
        records: tuple[ToolRecord, ...],
        produced_by: Caller,
        task_id: str | None,
    ) -> EvidenceItem: ...


class GapSink(Protocol):
    async def add_gap(self, gap: DataGapDraft) -> DataGap: ...


class ToolBudget(Protocol):
    """Atomic Tool_Call Budget counter (Req 15.9, 18.2)."""

    def try_reserve(self) -> bool:
        """Count one Tool_Call if the budget allows it; return False when it is exhausted."""

    @property
    def used(self) -> int: ...


class TerminationSignal(Protocol):
    def signal(self, reason: TerminationReason) -> None: ...
