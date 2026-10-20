"""Serializable Blackboard state (Req 3.10)."""

from .base import AwareDatetime, StrictModel
from .budget import BudgetUsage
from .claims import LabelledClaim
from .critic import EvidenceRequest
from .evidence import EvidenceItem, RetainedRecords
from .gaps import DataGap
from .hypothesis import ConfidenceAdjustment, HypothesisRevision
from .incident import IncidentClaim, IncidentReport
from .tasks import Task
from .vocab import TerminationReason, Verdict


class TerminationRecord(StrictModel):
    reason: TerminationReason
    at: AwareDatetime
    usage: BudgetUsage


class BlackboardState(StrictModel):
    run_id: str
    incident_id: str
    incident_report: IncidentReport
    claims: tuple[IncidentClaim, ...] = ()
    evidence: tuple[EvidenceItem, ...] = ()
    retained: tuple[RetainedRecords, ...] = ()
    hypotheses: dict[str, tuple[HypothesisRevision, ...]] = {}
    active_hypothesis_ids: tuple[str, ...] = ()
    last_reasoning_task_id: str | None = None
    adjustments: tuple[ConfidenceAdjustment, ...] = ()
    findings: tuple[LabelledClaim, ...] = ()
    tasks: tuple[Task, ...] = ()
    evidence_requests: tuple[EvidenceRequest, ...] = ()
    data_gaps: tuple[DataGap, ...] = ()
    budget: BudgetUsage = BudgetUsage()
    termination: TerminationRecord | None = None
    critic_acceptance: str | None = None
    verdict: Verdict | None = None
