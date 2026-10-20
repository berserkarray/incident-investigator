"""Read-only view of the Blackboard that Agents receive. Agents never write to it directly."""

from typing import Protocol

from ..models.claims import LabelledClaim
from ..models.critic import EvidenceRequest
from ..models.evidence import EvidenceItem, LedgerView
from ..models.gaps import DataGap
from ..models.hypothesis import HypothesisRevision
from ..models.incident import IncidentClaim
from ..models.blackboard import TerminationRecord
from ..models.budget import BudgetUsage
from ..models.hypothesis import ConfidenceAdjustment
from ..models.tools import ToolRecord
from ..models.vocab import Verdict


class BlackboardReader(Protocol):
    @property
    def usage(self) -> BudgetUsage: ...

    @property
    def verdict(self) -> Verdict | None: ...

    @property
    def termination(self) -> TerminationRecord | None: ...

    def adjustments(self) -> tuple[ConfidenceAdjustment, ...]: ...

    def evidence(self) -> tuple[EvidenceItem, ...]: ...

    def records_for(self, evidence_id: str) -> tuple[ToolRecord, ...]: ...

    def record_by_source_id(self, record_id: str) -> ToolRecord | None: ...

    def active_hypotheses(self) -> tuple[HypothesisRevision, ...]: ...

    def superseded_hypotheses(self) -> tuple[HypothesisRevision, ...]: ...

    def claims(self) -> tuple[IncidentClaim, ...]: ...

    def findings(self) -> tuple[LabelledClaim, ...]: ...

    def data_gaps(self) -> tuple[DataGap, ...]: ...

    def evidence_requests(self) -> tuple[EvidenceRequest, ...]: ...

    def ledger_view(self) -> LedgerView: ...
