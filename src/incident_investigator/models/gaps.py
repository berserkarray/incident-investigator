"""Data_Gaps: evidence a System_Under_Test could not obtain (Req 16.2, 17, 18.2, 25.9)."""

from .base import AwareDatetime, StrictModel, TimeRange, canonical_json
from .tools import ToolInput
from .vocab import Caller, ToolName


class DataGapDraft(StrictModel):
    """A Data_Gap before the Blackboard assigns its ID and timestamp."""

    source: ToolName | Caller
    query: ToolInput | None = None
    task_id: str | None = None
    step: int | None = None
    reason: str  # error code | failure kind | "unfulfilled_evidence_request:{id}:{termination}"
    affected_time_range: TimeRange | None = None
    after_verdict: bool = False


class DataGap(DataGapDraft):
    gap_id: str
    recorded_at: AwareDatetime

    def dedupe_key(self) -> tuple[str, str]:
        """Gaps with the same source and the same query or Task count once (Req 17.4)."""
        if self.query is not None:
            detail = canonical_json(self.query)
        elif self.task_id is not None:
            detail = f"task:{self.task_id}"
        elif self.step is not None:
            detail = f"step:{self.step}"
        else:
            detail = f"reason:{self.reason}"
        return (str(self.source), detail)
