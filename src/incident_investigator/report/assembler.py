"""ReportAssembler: builds the Investigation_Report from Blackboard state (Req 12.2, 17.6, 17.9, 19.3-19.7, 20).

Every report path (Reporting_Agent, Deterministic_Report_Generator, Baseline_Agent) goes through
here, so Verdict, Termination_Reason, Hypotheses, Data_Gaps and claim statuses are copied unchanged.
"""

from collections.abc import Sequence

from ..models.budget import BudgetLimits
from ..models.claims import LabelledClaim
from ..models.critic import EvidenceRequestStatus
from ..models.hypothesis import ConfidenceAdjustment, HypothesisRevision
from ..models.report import (
    AssumptionEntry,
    BudgetLimitNote,
    BudgetUsageReport,
    BuiltBy,
    EvidenceAppendixEntry,
    FactEntry,
    IncidentReference,
    InvestigationReport,
    MissingEvidenceEntry,
    RecommendedAction,
    ReportHypothesis,
    UsageLine,
)
from ..models.vocab import ClaimLabel, SystemName, TerminationReason, Verdict
from ..state.blackboard import Blackboard

_LIMIT_FOR_REASON = {
    TerminationReason.MAX_CRITIC_ROUNDS: "critic_rounds",
    TerminationReason.TOOL_CALL_BUDGET_EXHAUSTED: "tool_calls",
    TerminationReason.TOKEN_BUDGET_EXHAUSTED: "tokens",
    TerminationReason.WALL_CLOCK_TIMEOUT: "wall_clock_s",
}


def to_report_hypotheses(
    revisions: Sequence[HypothesisRevision], adjustments: Sequence[ConfidenceAdjustment]
) -> tuple[ReportHypothesis, ...]:
    adjusted = {(a.hypothesis_id, a.revision): a.adjusted_confidence for a in adjustments}
    return tuple(
        ReportHypothesis(
            hypothesis_id=r.hypothesis_id,
            rank=r.rank,
            revision=r.revision,
            component=r.component,
            cause_type=r.cause_type,
            statement=r.statement,
            supporting_evidence_ids=r.supporting_evidence_ids,
            contradicting_evidence_ids=r.contradicting_evidence_ids,
            confidence=adjusted.get((r.hypothesis_id, r.revision), r.confidence),
            raw_confidence=r.confidence,
            evidence_sufficient=r.evidence_sufficient,
            missing_evidence=r.missing_evidence,
            contributing_factors=r.contributing_factors,
        )
        for r in revisions
    )


def ranked_hypotheses(board: Blackboard) -> tuple[ReportHypothesis, ...]:
    """The active set in rank order with adjusted Confidence."""
    return to_report_hypotheses(board.active_hypotheses(), board.adjustments())


def missing_evidence_entries(board: Blackboard) -> tuple[MissingEvidenceEntry, ...]:
    """Unfulfilled Evidence_Requests, Data_Gaps and Reasoning missing-evidence entries (Req 19.5)."""
    entries: list[MissingEvidenceEntry] = []
    for request in board.evidence_requests():
        if request.status not in (EvidenceRequestStatus.COMPLETED, EvidenceRequestStatus.DUPLICATE):
            entries.append(
                MissingEvidenceEntry(
                    source="evidence_request",
                    hypothesis_id=request.hypothesis_id,
                    description=f"{request.question} (suggested: {request.suggested_tool})",
                    ref=request.request_id,
                )
            )
    for gap in board.data_gaps():
        entries.append(
            MissingEvidenceEntry(
                source="data_gap", hypothesis_id=None, description=f"{gap.source}: {gap.reason}", ref=gap.gap_id
            )
        )
    for hypothesis in board.active_hypotheses():
        for item in hypothesis.missing_evidence:
            entries.append(
                MissingEvidenceEntry(
                    source="reasoning_agent", hypothesis_id=hypothesis.hypothesis_id, description=item.description
                )
            )
    return tuple(entries)


def _claim_entries(claims: Sequence[LabelledClaim]) -> tuple[tuple[FactEntry, ...], tuple[AssumptionEntry, ...]]:
    facts, assumptions = [], []
    for claim in claims:
        if claim.label is ClaimLabel.FACT:
            facts.append(
                FactEntry(
                    claim_id=claim.claim_id,
                    producer=claim.producer,
                    text=claim.body.text,
                    evidence_ids=claim.body.evidence_ids,
                )
            )
        else:
            assumptions.append(AssumptionEntry(claim_id=claim.claim_id, producer=claim.producer, text=claim.body.text))
    return tuple(facts), tuple(assumptions)


class ReportAssembler:
    def assemble(
        self,
        board: Blackboard,
        *,
        system: SystemName,
        built_by: BuiltBy,
        actions: Sequence[RecommendedAction],
        limits: BudgetLimits,
        extra_claims: Sequence[LabelledClaim] = (),
    ) -> InvestigationReport:
        termination = board.termination
        if termination is None or board.verdict is None:
            raise RuntimeError("a report is assembled only after termination and the Verdict are recorded")
        report = board.incident_report
        usage = board.usage
        limit_name = _LIMIT_FOR_REASON.get(termination.reason)
        budget_limit = None
        if limit_name is not None:
            configured = {
                "critic_rounds": limits.critic_rounds,
                "tool_calls": limits.tool_calls,
                "tokens": limits.tokens,
                "wall_clock_s": limits.wall_clock_s,
            }[limit_name]
            at_end = termination.usage
            used = {
                "critic_rounds": at_end.critic_rounds,
                "tool_calls": at_end.tool_calls,
                "tokens": at_end.total_tokens,
                "wall_clock_s": at_end.elapsed_s,
            }[limit_name]
            budget_limit = BudgetLimitNote(limit=limit_name, configured=configured, used_at_loop_end=used)
        facts, assumptions = _claim_entries((*board.findings(), *extra_claims))
        return InvestigationReport(
            system=system,
            built_by=built_by,
            incident=IncidentReference(
                incident_id=board.incident_id,
                title=report.title,
                affected_service=report.affected_service,
                start_time=report.start_time,
                severity=report.severity,
            ),
            verdict=board.verdict,
            termination_reason=termination.reason,
            budget_limit=budget_limit,
            hypotheses=ranked_hypotheses(board),
            superseded_hypotheses=to_report_hypotheses(board.superseded_hypotheses(), board.adjustments()),
            facts=facts,
            assumptions=assumptions,
            data_gaps=board.data_gaps(),
            recommended_actions=tuple(actions),
            claim_statuses=board.claims(),
            missing_evidence=missing_evidence_entries(board) if board.verdict is Verdict.INCONCLUSIVE else (),
            budget_usage=BudgetUsageReport(
                tool_calls=UsageLine(used=usage.tool_calls, limit=limits.tool_calls),
                tokens=UsageLine(used=usage.total_tokens, limit=limits.tokens),
                input_tokens=usage.tokens.input_tokens,
                output_tokens=usage.tokens.output_tokens,
                critic_rounds=UsageLine(used=usage.critic_rounds, limit=limits.critic_rounds),
                wall_clock_s=UsageLine(used=round(usage.elapsed_s, 3), limit=limits.wall_clock_s),
            ),
            evidence_appendix=tuple(
                EvidenceAppendixEntry(
                    evidence_id=e.evidence_id,
                    tool=e.tool,
                    query=e.query,
                    observation=e.observation,
                    reliability=e.reliability,
                )
                for e in board.evidence()
            ),
        )
