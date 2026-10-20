"""Run_Trace events (Req 6.5, 29.3-29.6)."""

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from .base import AwareDatetime, JsonValue, StrictModel
from .budget import BudgetUsage
from .critic import EvidenceRequest
from .gaps import DataGap
from .hypothesis import ConfidenceAdjustment
from .report import UsageLine
from .vocab import (
    AgentName,
    Caller,
    CallKind,
    ErrorCode,
    FailureKind,
    SystemName,
    TaskState,
    TerminationReason,
    ToolName,
    Verdict,
)


class _Event(StrictModel):
    seq: int = Field(ge=0)
    at: AwareDatetime


class RunStarted(_Event):
    type: Literal["run_started"] = "run_started"
    system: SystemName
    incident_id: str
    seed: int
    effective_config: dict[str, JsonValue]


class TaskTransition(_Event):
    type: Literal["task_transition"] = "task_transition"
    task_id: str
    agent: AgentName
    from_state: TaskState
    to_state: TaskState


class ToolCallEvent(_Event):
    type: Literal["tool_call"] = "tool_call"
    caller: Caller
    task_id: str | None
    tool: ToolName
    input: dict[str, JsonValue]
    attempt: int = Field(ge=1)
    trial: bool = False
    error_code: ErrorCode | None = None
    partial: bool = False
    stale: bool = False
    truncated: bool = False
    duration_s: float = Field(ge=0)


class ToolRejected(_Event):
    type: Literal["tool_rejected"] = "tool_rejected"
    caller: Caller
    task_id: str | None
    tool: ToolName | str
    input: dict[str, JsonValue]
    code: ErrorCode
    invalid_fields: tuple[str, ...] = ()


class LLMCallEvent(_Event):
    type: Literal["llm_call"] = "llm_call"
    caller: Caller
    call_kind: CallKind
    attempt: int = Field(ge=1)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    duration_s: float = Field(ge=0)
    outcome: Literal["ok", "error"]
    error_category: str | None = None


class CitationViolationEvent(_Event):
    type: Literal["citation_violation"] = "citation_violation"
    producer: Caller
    target_kind: Literal["claim", "hypothesis", "recommended_action", "claim_status"]
    target_id: str
    removed_evidence_id: str | None = None
    removed_hypothesis_id: str | None = None
    kept_prior_status: bool = False


class DuplicateDiscarded(_Event):
    type: Literal["duplicate_discarded"] = "duplicate_discarded"
    request: EvidenceRequest
    matched_task_id: str | None = None
    matched_request_id: str | None = None


class DefaultGraphFallback(_Event):
    type: Literal["default_graph_fallback"] = "default_graph_fallback"
    cause: Literal["cycle", "duplicate_task_id", "unknown_dependency", "structure", "validation_failed", "llm_failed"]
    detail: str


class CriticDecision(_Event):
    type: Literal["critic_decision"] = "critic_decision"
    critic_round: int = Field(ge=1)
    accepted_hypothesis_id: str | None = None
    evidence_requests: tuple[EvidenceRequest, ...] = ()


class AgentFailureEvent(_Event):
    type: Literal["agent_failure"] = "agent_failure"
    agent: Caller
    task_id: str | None = None
    step: int | None = None
    attempt: Literal[1, 2]
    failure_kind: FailureKind
    message: str


class DataGapRecorded(_Event):
    type: Literal["data_gap_recorded"] = "data_gap_recorded"
    gap: DataGap


class HypothesisRevised(_Event):
    type: Literal["hypothesis_revised"] = "hypothesis_revised"
    hypothesis_id: str
    revision: int


class ConfidenceApplied(_Event):
    type: Literal["confidence_applied"] = "confidence_applied"
    adjustments: tuple[ConfidenceAdjustment, ...]
    gap_count: int
    penalty: float


class VerdictSet(_Event):
    type: Literal["verdict_set"] = "verdict_set"
    verdict: Verdict
    conditions: dict[str, bool]


class BaselineStepEvent(_Event):
    type: Literal["baseline_step"] = "baseline_step"
    step: int = Field(ge=1)
    action: Literal["tool", "final"]
    tool: ToolName | None = None


class Termination(_Event):
    type: Literal["termination"] = "termination"
    reason: TerminationReason
    usage: BudgetUsage


class RunEnded(_Event):
    type: Literal["run_ended"] = "run_ended"
    termination_reason: TerminationReason
    critic_rounds: UsageLine
    tool_calls: UsageLine
    input_tokens: int
    output_tokens: int
    tokens: UsageLine
    wall_clock_s: UsageLine


TraceEvent = Annotated[
    RunStarted
    | TaskTransition
    | ToolCallEvent
    | ToolRejected
    | LLMCallEvent
    | CitationViolationEvent
    | DuplicateDiscarded
    | DefaultGraphFallback
    | CriticDecision
    | AgentFailureEvent
    | DataGapRecorded
    | HypothesisRevised
    | ConfidenceApplied
    | VerdictSet
    | BaselineStepEvent
    | Termination
    | RunEnded,
    Field(discriminator="type"),
]
TRACE_EVENT_ADAPTER: TypeAdapter = TypeAdapter(TraceEvent)


class RunTrace(StrictModel):
    run_id: str
    system: SystemName
    events: tuple[TraceEvent, ...] = ()

    def of_type(self, event_type: str) -> tuple:
        return tuple(e for e in self.events if e.type == event_type)
