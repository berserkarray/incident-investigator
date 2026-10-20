"""Evidence_Items, Observation_Summaries and the Observation format (Req 3, 25.10)."""

from pydantic import Field

from .base import AwareDatetime, JsonValue, StrictModel, TimeRange
from .tools import ToolError, ToolInput, ToolRecord
from .vocab import Caller, Reliability, ToolName


class TopValue(StrictModel):
    value: str
    count: int = Field(ge=1)
    first_seen: AwareDatetime | None = None


class MetricStats(StrictModel):
    min: float
    max: float
    mean: float
    first: float
    last: float
    max_at: AwareDatetime
    min_at: AwareDatetime


class ObservationSummary(StrictModel):
    """Compact, deterministic summary of one Tool_Result. Never contains full records."""

    record_count: int = Field(ge=0)
    time_span: TimeRange | None = None
    top_values: tuple[TopValue, ...] = ()
    key_attributes: dict[str, tuple[str, ...]] = {}
    metric_stats: MetricStats | None = None
    note: str | None = None
    truncated: bool = False


class EvidenceItem(StrictModel):
    """One append-only Run_Ledger entry, created only by the Tool_Layer."""

    evidence_id: str
    seq: int = Field(ge=1)
    tool: ToolName
    query: ToolInput
    observation: ObservationSummary
    time_range: TimeRange | None
    reliability: Reliability
    source_record_ids: tuple[str, ...]
    produced_by: Caller
    task_id: str | None
    recorded_at: AwareDatetime

    @property
    def historical(self) -> bool:
        """Historical_Evidence describes a past incident, not the current one."""
        return self.tool is ToolName.SEARCH_INCIDENTS


class RetainedRecords(StrictModel):
    """The full records behind one Evidence_Item, retrievable without a Tool_Call (Req 3.12)."""

    evidence_id: str
    records: tuple[ToolRecord, ...]


class Observation(StrictModel):
    """What the Tool_Layer returns to a Specialist_Agent or the Baseline_Agent (Req 25.10)."""

    evidence_id: str | None
    tool: ToolName
    query: ToolInput | None
    raw_params: dict[str, JsonValue]
    summary: ObservationSummary
    records: tuple[ToolRecord, ...] = ()
    error: ToolError | None = None
    partial: bool = False
    stale: bool = False
    as_of: AwareDatetime | None = None
    truncated: bool = False


class LedgerEntry(StrictModel):
    tool: ToolName
    reliability: Reliability

    @property
    def historical(self) -> bool:
        return self.tool is ToolName.SEARCH_INCIDENTS


class LedgerView(StrictModel):
    """Light index of the Run_Ledger used by validators and verdict rules."""

    items: dict[str, LedgerEntry] = {}

    def has(self, evidence_id: str) -> bool:
        return evidence_id in self.items

    def is_historical(self, evidence_id: str) -> bool:
        entry = self.items.get(evidence_id)
        return entry is not None and entry.historical

    def is_complete_current(self, evidence_id: str) -> bool:
        """In the ledger, reliability ``complete`` and not Historical_Evidence (Req 19.1, 25.5)."""
        entry = self.items.get(evidence_id)
        return entry is not None and not entry.historical and entry.reliability is Reliability.COMPLETE
