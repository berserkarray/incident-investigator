"""Structured LLM outputs of every Agent and the Baseline_Agent.

Only rules that need no run-time context live here. Rules that depend on the Blackboard
(valid Components, the active Hypotheses, configured maxima, Tool fit) are context validators
applied by the Structured_Output_Validator.
"""

from typing import Generic, TypeVar

from pydantic import BaseModel, Field, model_validator

from .base import JsonValue, NonBlank500, NonBlank1000, StrictModel, TimeRange
from .claims import ClaimDraft, DependencyClaim, ObservabilityClaim
from .incident import ClaimStatusUpdate
from .vocab import ActionCategory, AgentName, CauseType, ConcernCategory, ToolName, Verdict


class ToolAction(StrictModel):
    tool: ToolName
    params: dict[str, JsonValue]


class ObservabilityFindings(StrictModel):
    claims: tuple[ObservabilityClaim, ...] = Field((), max_length=20)


class DependencyFindings(StrictModel):
    claims: tuple[DependencyClaim, ...] = Field((), max_length=20)


F = TypeVar("F", bound=BaseModel)


class SpecialistStep(StrictModel, Generic[F]):
    """One Specialist step: either a Tool_Request or the Task findings."""

    reasoning: str = Field("", max_length=1000)
    tool_request: ToolAction | None = None
    findings: F | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "SpecialistStep":
        if (self.tool_request is None) == (self.findings is None):
            raise ValueError("give exactly one of tool_request or findings")
        return self


ObservabilityStep = SpecialistStep[ObservabilityFindings]
DependencyStep = SpecialistStep[DependencyFindings]


class PlannedTask(StrictModel):
    task_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    agent: AgentName
    depends_on: tuple[str, ...] = ()
    focus: str = Field("", max_length=500)
    time_range: TimeRange | None = None


class PlanOutput(StrictModel):
    tasks: tuple[PlannedTask, ...] = Field(min_length=1, max_length=20)


class MissingEvidence(StrictModel):
    description: NonBlank500
    suggested_tool: ToolName | None = None


class HypothesisDraft(StrictModel):
    hypothesis_id: str | None = None  # Baseline only; Reasoning IDs are assigned by code
    component: str = Field(min_length=1, max_length=100)
    cause_type: CauseType
    statement: NonBlank500
    supporting_evidence_ids: tuple[str, ...] = ()
    contradicting_evidence_ids: tuple[str, ...] = ()
    confidence: float = Field(ge=0.0, le=1.0)
    rank: int = Field(ge=1)
    evidence_sufficient: bool
    missing_evidence: tuple[MissingEvidence, ...] = Field((), max_length=10)
    contributing_factors: tuple[str, ...] = Field((), max_length=10)

    @model_validator(mode="after")
    def _disjoint_citations(self) -> "HypothesisDraft":
        shared = set(self.supporting_evidence_ids) & set(self.contradicting_evidence_ids)
        if shared:
            raise ValueError(f"evidence cited as both supporting and contradicting: {sorted(shared)}")
        return self

    @property
    def label(self) -> tuple[str, CauseType]:
        return (self.component, self.cause_type)


def _check_ranking(hypotheses: tuple[HypothesisDraft, ...]) -> None:
    """Unique Root_Cause_Labels, ranks 1..N, Confidence non-increasing by rank (Req 10.1, 10.3)."""
    if not hypotheses:
        return
    labels = [h.label for h in hypotheses]
    if len(labels) != len(set(labels)):
        raise ValueError("two hypotheses share a Root_Cause_Label (component, cause_type)")
    ranks = sorted(h.rank for h in hypotheses)
    if ranks != list(range(1, len(hypotheses) + 1)):
        raise ValueError(f"ranks must be exactly 1..{len(hypotheses)}, got {ranks}")
    ordered = sorted(hypotheses, key=lambda h: h.rank)
    for higher, lower in zip(ordered, ordered[1:]):
        if lower.confidence > higher.confidence:
            raise ValueError("confidence must not increase as rank increases")


class ReasoningOutput(StrictModel):
    hypotheses: tuple[HypothesisDraft, ...] = Field(min_length=2, max_length=5)
    claim_statuses: tuple[ClaimStatusUpdate, ...] = ()

    @model_validator(mode="after")
    def _ranking(self) -> "ReasoningOutput":
        _check_ranking(self.hypotheses)
        return self


class EvidenceRequestDraft(StrictModel):
    target: AgentName
    hypothesis_id: str
    concern: ConcernCategory
    question: NonBlank500
    suggested_tool: ToolName
    suggested_params: dict[str, JsonValue]
    rationale: NonBlank1000

    @model_validator(mode="after")
    def _specialist_target(self) -> "EvidenceRequestDraft":
        if self.target not in (AgentName.OBSERVABILITY, AgentName.DEPENDENCY):
            raise ValueError("target must be observability or dependency")
        return self


class CriticOutput(StrictModel):
    accept_hypothesis_id: str | None = None
    evidence_requests: tuple[EvidenceRequestDraft, ...] = ()

    @model_validator(mode="after")
    def _accept_xor_requests(self) -> "CriticOutput":
        if (self.accept_hypothesis_id is None) == (not self.evidence_requests):
            raise ValueError("give exactly one of accept_hypothesis_id or evidence_requests")
        return self


class RecommendedActionDraft(StrictModel):
    text: NonBlank1000
    category: ActionCategory
    hypothesis_id: str
    evidence_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _evidence_unless_investigation(self) -> "RecommendedActionDraft":
        if self.category is not ActionCategory.FURTHER_INVESTIGATION and not self.evidence_ids:
            raise ValueError(f"a {self.category} action must cite at least one Evidence_ID")
        return self


class ReportingOutput(StrictModel):
    summary_claims: tuple[ClaimDraft, ...] = Field((), max_length=10)
    recommended_actions: tuple[RecommendedActionDraft, ...] = ()


class BaselineFinalAnswer(StrictModel):
    outcome: Verdict
    hypotheses: tuple[HypothesisDraft, ...] = Field((), max_length=5)
    claim_statuses: tuple[ClaimStatusUpdate, ...] = ()
    claims: tuple[ClaimDraft, ...] = ()
    recommended_actions: tuple[RecommendedActionDraft, ...] = ()

    @model_validator(mode="after")
    def _baseline_rules(self) -> "BaselineFinalAnswer":
        ids = [h.hypothesis_id for h in self.hypotheses]
        if any(not i for i in ids):
            raise ValueError("every baseline hypothesis needs a hypothesis_id")
        if len(ids) != len(set(ids)):
            raise ValueError("baseline hypothesis_ids must be unique")
        _check_ranking(self.hypotheses)
        return self


class BaselineStep(StrictModel):
    reasoning_note: str = Field("", max_length=1000)
    tool_request: ToolAction | None = None
    final_answer: BaselineFinalAnswer | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "BaselineStep":
        if (self.tool_request is None) == (self.final_answer is None):
            raise ValueError("give exactly one of tool_request or final_answer")
        return self
