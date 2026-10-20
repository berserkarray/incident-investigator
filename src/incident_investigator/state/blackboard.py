"""The Blackboard: shared investigation state of one Run (Req 1.4, 3).

Evidence_Items are append-only and created only by the Tool_Layer. Agents read the Blackboard;
the Orchestrator (or the Baseline_Agent) commits their outputs after citation validation.
Compound writes run under one asyncio.Lock so concurrent writers never lose an update.
"""

import asyncio
from collections.abc import Iterable, Sequence
from typing import NoReturn

from ..clock import Clock
from ..errors import ImmutableEvidenceError
from ..models.base import TimeRange
from ..models.blackboard import BlackboardState, TerminationRecord
from ..models.budget import BudgetUsage, TokenCount
from ..models.claims import LabelledClaim
from ..models.critic import EvidenceRequest
from ..models.evidence import EvidenceItem, LedgerEntry, LedgerView, ObservationSummary, RetainedRecords
from ..models.gaps import DataGap, DataGapDraft
from ..models.hypothesis import ConfidenceAdjustment, HypothesisRevision
from ..models.incident import IncidentClaim, IncidentClaimKind, IncidentReport
from ..models.outputs import HypothesisDraft
from ..models.tasks import Task
from ..models.tools import ToolInput, ToolRecord, record_source_ids
from ..models.vocab import Caller, Reliability, TerminationReason, ToolName, Verdict
from ..tracing import TraceRecorder


def _numbered(ids: Iterable[str], prefix: str) -> list[int]:
    """The numbers of IDs shaped ``<prefix><digits>`` (``H3`` -> 3); other IDs are ignored."""
    numbers = []
    for value in ids:
        digits = value[len(prefix):]
        if value.startswith(prefix) and digits.isdigit():
            numbers.append(int(digits))
    return numbers


def initial_claims(report: IncidentReport) -> tuple[IncidentClaim, ...]:
    """One claim per symptom, then the affected service, then the start time (Req 1.4)."""
    claims = [
        IncidentClaim(claim_id=f"IC{i}", kind=IncidentClaimKind.SYMPTOM, text=symptom)
        for i, symptom in enumerate(report.symptoms, start=1)
    ]
    n = len(claims)
    claims.append(
        IncidentClaim(
            claim_id=f"IC{n + 1}",
            kind=IncidentClaimKind.AFFECTED_SERVICE,
            text=f"The affected service is {report.affected_service}.",
        )
    )
    claims.append(
        IncidentClaim(
            claim_id=f"IC{n + 2}",
            kind=IncidentClaimKind.START_TIME,
            text=f"The incident started at {report.start_time.isoformat()}.",
        )
    )
    return tuple(claims)


class Blackboard:
    def __init__(
        self,
        run_id: str,
        incident_id: str,
        report: IncidentReport,
        clock: Clock,
        trace: TraceRecorder | None = None,
    ):
        self.run_id = run_id
        self.incident_id = incident_id
        self.incident_report = report
        self._clock = clock
        self._trace = trace
        self._lock = asyncio.Lock()
        self._claims: dict[str, IncidentClaim] = {}
        self._evidence: list[EvidenceItem] = []
        self._retained: dict[str, tuple[ToolRecord, ...]] = {}
        self._by_source_id: dict[str, ToolRecord] = {}
        self._hypotheses: dict[str, list[HypothesisRevision]] = {}
        self._active: tuple[str, ...] = ()
        self._last_reasoning_task_id: str | None = None
        self._adjustments: tuple[ConfidenceAdjustment, ...] = ()
        self._findings: list[LabelledClaim] = []
        self._tasks: dict[str, Task] = {}
        self._requests: dict[str, EvidenceRequest] = {}
        self._gaps: list[DataGap] = []
        self._usage = BudgetUsage()
        self._termination: TerminationRecord | None = None
        self._acceptance: str | None = None
        self._verdict: Verdict | None = None
        self._next_hypothesis = 1
        self._next_claim = 1

    # Writes

    async def record_initial_claims(self) -> tuple[IncidentClaim, ...]:
        async with self._lock:
            if not self._claims:
                for claim in initial_claims(self.incident_report):
                    self._claims[claim.claim_id] = claim
            return tuple(self._claims.values())

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
    ) -> EvidenceItem:
        async with self._lock:
            seq = len(self._evidence) + 1
            source_ids: list[str] = []
            seen: set[str] = set()
            for record in records:
                for rid in record_source_ids(record):
                    if rid not in seen:
                        seen.add(rid)
                        source_ids.append(rid)
                    self._by_source_id.setdefault(rid, record)
            item = EvidenceItem(
                evidence_id=f"E{seq:04d}",
                seq=seq,
                tool=tool,
                query=query,
                observation=summary,
                time_range=time_range,
                reliability=reliability,
                source_record_ids=tuple(source_ids),
                produced_by=produced_by,
                task_id=task_id,
                recorded_at=self._clock.now(),
            )
            self._evidence.append(item)
            self._retained[item.evidence_id] = tuple(records)
            return item

    def update_evidence(self, *args, **kwargs) -> NoReturn:
        raise ImmutableEvidenceError("Evidence_Items cannot be modified after insertion")

    def delete_evidence(self, *args, **kwargs) -> NoReturn:
        raise ImmutableEvidenceError("Evidence_Items cannot be deleted")

    async def add_gap(self, gap: DataGapDraft) -> DataGap:
        async with self._lock:
            recorded = DataGap(
                **gap.model_dump(), gap_id=f"G{len(self._gaps) + 1:03d}", recorded_at=self._clock.now()
            )
            self._gaps.append(recorded)
        if self._trace is not None:
            self._trace.record("data_gap_recorded", gap=recorded)
        return recorded

    async def add_usage(self, caller: Caller, tokens: TokenCount) -> BudgetUsage:
        async with self._lock:
            per_caller = dict(self._usage.per_caller)
            key = str(caller)
            per_caller[key] = per_caller.get(key, TokenCount()).plus(tokens)
            self._usage = self._usage.model_copy(
                update={"tokens": self._usage.tokens.plus(tokens), "per_caller": per_caller}
            )
            return self._usage

    # Budget counters. Each is one read-modify-write with no await inside, so it is atomic under
    # asyncio and cannot interleave with ``add_usage`` (Req 3.8).

    def reserve_tool_call(self, limit: int) -> bool:
        """Count one Tool_Call if fewer than ``limit`` were made."""
        if self._usage.tool_calls >= limit:
            return False
        self._usage = self._usage.model_copy(update={"tool_calls": self._usage.tool_calls + 1})
        return True

    def increment_critic_round(self) -> int:
        self._usage = self._usage.model_copy(update={"critic_rounds": self._usage.critic_rounds + 1})
        return self._usage.critic_rounds

    def set_elapsed(self, seconds: float) -> None:
        self._usage = self._usage.model_copy(update={"elapsed_s": max(0.0, seconds)})

    def next_hypothesis_id(self) -> str:
        hypothesis_id = f"H{self._next_hypothesis}"
        self._next_hypothesis += 1
        return hypothesis_id

    async def add_hypothesis_revisions(
        self, task_id: str | None, drafts: Sequence[tuple[str, HypothesisDraft]]
    ) -> tuple[HypothesisRevision, ...]:
        """Append revision ``max + 1`` per Hypothesis ID; the output becomes the active set (Req 3.9, D8)."""
        async with self._lock:
            revisions = []
            for hypothesis_id, draft in sorted(drafts, key=lambda pair: pair[1].rank):
                history = self._hypotheses.setdefault(hypothesis_id, [])
                revision = HypothesisRevision(
                    hypothesis_id=hypothesis_id,
                    revision=len(history) + 1,
                    task_id=task_id,
                    recorded_at=self._clock.now(),
                    component=draft.component,
                    cause_type=draft.cause_type,
                    statement=draft.statement,
                    supporting_evidence_ids=draft.supporting_evidence_ids,
                    contradicting_evidence_ids=draft.contradicting_evidence_ids,
                    confidence=draft.confidence,
                    rank=draft.rank,
                    evidence_sufficient=draft.evidence_sufficient,
                    missing_evidence=draft.missing_evidence,
                    contributing_factors=draft.contributing_factors,
                )
                history.append(revision)
                revisions.append(revision)
            self._active = tuple(r.hypothesis_id for r in revisions)
            self._last_reasoning_task_id = task_id
        if self._trace is not None:
            for revision in revisions:
                self._trace.record(
                    "hypothesis_revised", hypothesis_id=revision.hypothesis_id, revision=revision.revision
                )
        return tuple(revisions)

    async def set_claims(self, claims: Sequence[IncidentClaim]) -> None:
        """Replace claim records by ID with records already checked by the Citation_Validator."""
        async with self._lock:
            for claim in claims:
                if claim.claim_id in self._claims:
                    self._claims[claim.claim_id] = claim

    def next_finding_id(self) -> str:
        claim_id = f"F{self._next_claim:03d}"
        self._next_claim += 1
        return claim_id

    async def add_findings(self, claims: Sequence[LabelledClaim]) -> None:
        async with self._lock:
            self._findings.extend(claims)

    def put_task(self, task: Task) -> None:
        """Record a Task's current state. A single assignment, so it is atomic without the lock;
        the scheduler records transitions synchronously so no two can interleave."""
        self._tasks[task.task_id] = task

    async def upsert_task(self, task: Task) -> None:
        async with self._lock:
            self.put_task(task)

    def put_evidence_request(self, request: EvidenceRequest) -> None:
        self._requests[request.request_id] = request

    async def upsert_evidence_request(self, request: EvidenceRequest) -> None:
        async with self._lock:
            self.put_evidence_request(request)

    def evidence_request(self, request_id: str) -> EvidenceRequest | None:
        return self._requests.get(request_id)

    async def set_adjustments(self, adjustments: Sequence[ConfidenceAdjustment]) -> None:
        async with self._lock:
            if self._adjustments:
                raise RuntimeError("the Confidence_Policy is applied once per Run")
            self._adjustments = tuple(adjustments)

    async def set_termination(self, reason: TerminationReason) -> TerminationRecord:
        async with self._lock:
            if self._termination is None:
                self._termination = TerminationRecord(reason=reason, at=self._clock.now(), usage=self._usage)
            return self._termination

    async def set_verdict(self, verdict: Verdict, acceptance: str | None) -> None:
        async with self._lock:
            if self._verdict is not None:
                raise RuntimeError("the Verdict is set once per Run")
            self._verdict = verdict
            self._acceptance = acceptance

    # Reads

    def evidence(self) -> tuple[EvidenceItem, ...]:
        return tuple(self._evidence)

    def evidence_item(self, evidence_id: str) -> EvidenceItem | None:
        for item in self._evidence:
            if item.evidence_id == evidence_id:
                return item
        return None

    def records_for(self, evidence_id: str) -> tuple[ToolRecord, ...]:
        return self._retained.get(evidence_id, ())

    def record_by_source_id(self, record_id: str) -> ToolRecord | None:
        return self._by_source_id.get(record_id)

    def claims(self) -> tuple[IncidentClaim, ...]:
        return tuple(self._claims.values())

    def findings(self) -> tuple[LabelledClaim, ...]:
        return tuple(self._findings)

    def data_gaps(self) -> tuple[DataGap, ...]:
        return tuple(self._gaps)

    def evidence_requests(self) -> tuple[EvidenceRequest, ...]:
        return tuple(self._requests.values())

    def tasks(self) -> tuple[Task, ...]:
        return tuple(self._tasks.values())

    def hypothesis_history(self, hypothesis_id: str) -> tuple[HypothesisRevision, ...]:
        return tuple(self._hypotheses.get(hypothesis_id, ()))

    def all_hypothesis_ids(self) -> tuple[str, ...]:
        return tuple(self._hypotheses)

    def active_hypotheses(self) -> tuple[HypothesisRevision, ...]:
        latest = [self._hypotheses[h][-1] for h in self._active]
        return tuple(sorted(latest, key=lambda r: r.rank))

    def superseded_hypotheses(self) -> tuple[HypothesisRevision, ...]:
        return tuple(history[-1] for hid, history in self._hypotheses.items() if hid not in self._active)

    def adjustments(self) -> tuple[ConfidenceAdjustment, ...]:
        return self._adjustments

    @property
    def usage(self) -> BudgetUsage:
        return self._usage

    @property
    def termination(self) -> TerminationRecord | None:
        return self._termination

    @property
    def verdict(self) -> Verdict | None:
        return self._verdict

    @property
    def critic_acceptance(self) -> str | None:
        return self._acceptance

    @property
    def last_reasoning_task_id(self) -> str | None:
        return self._last_reasoning_task_id

    def ledger_view(self) -> LedgerView:
        return LedgerView(
            items={e.evidence_id: LedgerEntry(tool=e.tool, reliability=e.reliability) for e in self._evidence}
        )

    @classmethod
    def from_state(cls, state: BlackboardState, clock: Clock, trace: TraceRecorder | None = None) -> "Blackboard":
        """Rebuild a Blackboard from its serialized state; ``to_state()`` of the result equals ``state`` (Req 3.10)."""
        board = cls(state.run_id, state.incident_id, state.incident_report, clock, trace)
        board._claims = {c.claim_id: c for c in state.claims}
        board._evidence = list(state.evidence)
        board._retained = {r.evidence_id: tuple(r.records) for r in state.retained}
        for item in board._evidence:
            for record in board._retained.get(item.evidence_id, ()):
                for rid in record_source_ids(record):
                    board._by_source_id.setdefault(rid, record)
        board._hypotheses = {h: list(revisions) for h, revisions in state.hypotheses.items()}
        board._active = tuple(state.active_hypothesis_ids)
        board._last_reasoning_task_id = state.last_reasoning_task_id
        board._adjustments = tuple(state.adjustments)
        board._findings = list(state.findings)
        board._tasks = {t.task_id: t for t in state.tasks}
        board._requests = {r.request_id: r for r in state.evidence_requests}
        board._gaps = list(state.data_gaps)
        board._usage = state.budget
        board._termination = state.termination
        board._acceptance = state.critic_acceptance
        board._verdict = state.verdict
        board._next_hypothesis = 1 + max(_numbered(state.hypotheses, "H"), default=0)
        board._next_claim = 1 + max(_numbered((f.claim_id for f in state.findings), "F"), default=0)
        return board

    def to_state(self) -> BlackboardState:
        return BlackboardState(
            run_id=self.run_id,
            incident_id=self.incident_id,
            incident_report=self.incident_report,
            claims=self.claims(),
            evidence=self.evidence(),
            retained=tuple(RetainedRecords(evidence_id=e, records=r) for e, r in self._retained.items()),
            hypotheses={h: tuple(revs) for h, revs in self._hypotheses.items()},
            active_hypothesis_ids=self._active,
            last_reasoning_task_id=self._last_reasoning_task_id,
            adjustments=self._adjustments,
            findings=self.findings(),
            tasks=self.tasks(),
            evidence_requests=self.evidence_requests(),
            data_gaps=self.data_gaps(),
            budget=self._usage,
            termination=self._termination,
            critic_acceptance=self._acceptance,
            verdict=self._verdict,
        )
