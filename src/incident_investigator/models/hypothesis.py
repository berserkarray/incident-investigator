"""Hypothesis revisions and Confidence_Policy adjustments (Req 3.9, 17.4)."""

from pydantic import Field

from .base import AwareDatetime, StrictModel
from .outputs import MissingEvidence
from .vocab import CauseType


class HypothesisRevision(StrictModel):
    """One immutable revision of a Hypothesis. Revisions are numbered 1, 2, ... per Hypothesis ID."""

    hypothesis_id: str
    revision: int = Field(ge=1)
    task_id: str | None
    recorded_at: AwareDatetime
    component: str
    cause_type: CauseType
    statement: str
    supporting_evidence_ids: tuple[str, ...] = ()
    contradicting_evidence_ids: tuple[str, ...] = ()
    confidence: float = Field(ge=0.0, le=1.0)
    rank: int = Field(ge=1)
    evidence_sufficient: bool
    missing_evidence: tuple[MissingEvidence, ...] = ()
    contributing_factors: tuple[str, ...] = ()

    @property
    def label(self) -> tuple[str, CauseType]:
        return (self.component, self.cause_type)


class ConfidenceAdjustment(StrictModel):
    """Confidence after the per-Data_Gap penalty, stored apart so revisions stay immutable (D12)."""

    hypothesis_id: str
    revision: int
    raw_confidence: float = Field(ge=0.0, le=1.0)
    adjusted_confidence: float = Field(ge=0.0, le=1.0)
    penalty: float = Field(ge=0.0, le=1.0)
    gap_count: int = Field(ge=0)
