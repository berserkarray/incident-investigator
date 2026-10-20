"""Investigation_Report: one schema for both systems and every builder (Req 20, 21)."""

from typing import Literal

from pydantic import model_validator

from .base import AwareDatetime, StrictModel
from .evidence import ObservationSummary
from .gaps import DataGap
from .incident import IncidentClaim
from .outputs import MissingEvidence
from .tools import ToolInput
from .vocab import (
    ActionCategory,
    Caller,
    CauseType,
    Reliability,
    Severity,
    SystemName,
    TerminationReason,
    ToolName,
    Verdict,
)

BuiltBy = Literal["reporting_agent", "deterministic", "baseline"]


class IncidentReference(StrictModel):
    incident_id: str
    title: str
    affected_service: str
    start_time: AwareDatetime
    severity: Severity


class ReportHypothesis(StrictModel):
    hypothesis_id: str
    rank: int
    revision: int
    component: str
    cause_type: CauseType
    statement: str
    supporting_evidence_ids: tuple[str, ...] = ()
    contradicting_evidence_ids: tuple[str, ...] = ()
    confidence: float  # adjusted by the Confidence_Policy (Req 17.9)
    raw_confidence: float
    evidence_sufficient: bool
    missing_evidence: tuple[MissingEvidence, ...] = ()
    contributing_factors: tuple[str, ...] = ()


class FactEntry(StrictModel):
    claim_id: str
    producer: Caller
    text: str
    evidence_ids: tuple[str, ...]


class AssumptionEntry(StrictModel):
    claim_id: str
    producer: Caller
    text: str


class RecommendedAction(StrictModel):
    action_id: str
    text: str
    category: ActionCategory
    hypothesis_id: str
    evidence_ids: tuple[str, ...] = ()
    producer: BuiltBy


class MissingEvidenceEntry(StrictModel):
    source: Literal["evidence_request", "data_gap", "reasoning_agent"]
    hypothesis_id: str | None
    description: str
    ref: str | None = None


class UsageLine(StrictModel):
    used: float
    limit: float


class BudgetUsageReport(StrictModel):
    tool_calls: UsageLine
    tokens: UsageLine
    input_tokens: int
    output_tokens: int
    critic_rounds: UsageLine
    wall_clock_s: UsageLine


class BudgetLimitNote(StrictModel):
    limit: Literal["critic_rounds", "tool_calls", "tokens", "wall_clock_s"]
    configured: float
    used_at_loop_end: float


class EvidenceAppendixEntry(StrictModel):
    evidence_id: str
    tool: ToolName
    query: ToolInput
    observation: ObservationSummary
    reliability: Reliability


class InvestigationReport(StrictModel):
    schema_version: Literal[1] = 1
    system: SystemName
    built_by: BuiltBy
    incident: IncidentReference
    verdict: Verdict
    termination_reason: TerminationReason
    budget_limit: BudgetLimitNote | None = None
    hypotheses: tuple[ReportHypothesis, ...] = ()  # active set, rank 1..N
    superseded_hypotheses: tuple[ReportHypothesis, ...] = ()
    facts: tuple[FactEntry, ...] = ()
    assumptions: tuple[AssumptionEntry, ...] = ()
    data_gaps: tuple[DataGap, ...] = ()
    recommended_actions: tuple[RecommendedAction, ...] = ()
    claim_statuses: tuple[IncidentClaim, ...] = ()
    missing_evidence: tuple[MissingEvidenceEntry, ...] = ()
    budget_usage: BudgetUsageReport
    evidence_appendix: tuple[EvidenceAppendixEntry, ...] = ()

    @model_validator(mode="after")
    def _ranked(self) -> "InvestigationReport":
        ranks = [h.rank for h in self.hypotheses]
        if ranks != list(range(1, len(ranks) + 1)):
            raise ValueError("hypotheses must be listed in rank order 1..N")
        return self
