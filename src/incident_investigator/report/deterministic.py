"""Deterministic_Report_Generator: a report without LLM calls (Req 12.6, 12.7, 18.6, 25.6, design D7)."""

from ..models.budget import BudgetLimits
from ..models.critic import EvidenceRequestStatus
from ..models.report import BuiltBy, InvestigationReport, RecommendedAction
from ..models.vocab import ActionCategory, CauseType, SystemName, Verdict
from ..state.blackboard import Blackboard
from .assembler import ReportAssembler, ranked_hypotheses

MITIGATION_TEMPLATES: dict[CauseType, str] = {
    CauseType.CONFIG_ERROR: "Roll back or correct the configuration change on {component}.",
    CauseType.DEPLOY_REGRESSION: "Roll back the most recent deployment of {component}.",
    CauseType.FEATURE_FLAG_REGRESSION: "Disable the feature flag rollout affecting {component}.",
    CauseType.CODE_DEFECT: "Roll back or hotfix the defective code path in {component}.",
    CauseType.DATABASE_FAILOVER: "Confirm the new primary for {component} and repoint stale connections.",
    CauseType.CONNECTION_POOL_EXHAUSTION: "Raise the connection pool limit or shed load on {component}.",
    CauseType.CACHE_STAMPEDE: "Enable request coalescing or warm the cache on {component}.",
    CauseType.DATA_CORRUPTION: "Stop writes to the affected data in {component} and restore from a clean copy.",
    CauseType.MEMORY_LEAK: "Restart {component} on a rolling basis and roll back the change that introduced the leak.",
    CauseType.DISK_FULL: "Free disk space on {component} and add a capacity alert.",
    CauseType.RESOURCE_EXHAUSTION: "Add capacity to {component} or shed non-critical load.",
    CauseType.CAPACITY_LIMIT: "Raise the capacity limit of {component} or scale it out.",
    CauseType.LOAD_BALANCER_MISCONFIGURATION: "Restore the previous load balancer configuration for {component}.",
    CauseType.NETWORK_PARTITION: "Route traffic away from the partitioned network path to {component}.",
    CauseType.DNS_FAILURE: "Fail over DNS resolution for {component} to a healthy resolver or record.",
    CauseType.CERTIFICATE_EXPIRY: "Renew and deploy the TLS certificate used by {component}.",
    CauseType.THIRD_PARTY_OUTAGE: "Degrade gracefully or fail over away from {component} until the provider recovers.",
    CauseType.DEPENDENCY_TIMEOUT: "Tighten timeouts and add circuit breaking on calls to {component}.",
    CauseType.RATE_LIMITING: "Reduce request volume to {component} or request a higher rate limit.",
    CauseType.TRAFFIC_ATTACK: "Block or rate-limit the abusive traffic reaching {component}.",
    CauseType.CLOCK_SKEW: "Resynchronise clocks on {component} and verify NTP configuration.",
}


class DeterministicReportGenerator:
    def __init__(self, assembler: ReportAssembler | None = None):
        self._assembler = assembler or ReportAssembler()

    def actions(self, board: Blackboard, max_actions: int, producer: BuiltBy = "deterministic") -> tuple[RecommendedAction, ...]:
        ranked = ranked_hypotheses(board)
        if not ranked:
            return ()
        ledger = board.ledger_view()
        rank1 = ranked[0]
        drafts: list[tuple[str, ActionCategory, str, tuple[str, ...]]] = []
        if board.verdict is Verdict.CONCLUSIVE:
            support = tuple(e for e in rank1.supporting_evidence_ids if ledger.is_complete_current(e))
            text = MITIGATION_TEMPLATES[rank1.cause_type].format(component=rank1.component)
            drafts.append((text, ActionCategory.MITIGATION, rank1.hypothesis_id, support))
        active_ids = {h.hypothesis_id for h in ranked}
        for request in board.evidence_requests():
            if request.status not in (EvidenceRequestStatus.COMPLETED, EvidenceRequestStatus.DUPLICATE):
                hypothesis_id = request.hypothesis_id if request.hypothesis_id in active_ids else rank1.hypothesis_id
                drafts.append(
                    (
                        f"Answer: {request.question} using {request.suggested_tool}.",
                        ActionCategory.FURTHER_INVESTIGATION,
                        hypothesis_id,
                        (),
                    )
                )
        for hypothesis in ranked:
            for item in hypothesis.missing_evidence:
                drafts.append((item.description, ActionCategory.FURTHER_INVESTIGATION, hypothesis.hypothesis_id, ()))
        for gap in board.data_gaps():
            query = gap.query.model_dump_json() if gap.query is not None else (gap.task_id or "the failed step")
            drafts.append(
                (
                    f"Re-query {gap.source} for {query} ({gap.reason}).",
                    ActionCategory.FURTHER_INVESTIGATION,
                    rank1.hypothesis_id,
                    (),
                )
            )
        seen: set[str] = set()
        actions: list[RecommendedAction] = []
        for text, category, hypothesis_id, evidence_ids in drafts:
            if text in seen or (category is not ActionCategory.FURTHER_INVESTIGATION and not evidence_ids):
                continue
            seen.add(text)
            actions.append(
                RecommendedAction(
                    action_id=f"A{len(actions) + 1}",
                    text=text[:1000],
                    category=category,
                    hypothesis_id=hypothesis_id,
                    evidence_ids=evidence_ids,
                    producer=producer,
                )
            )
            if len(actions) >= max_actions:
                break
        return tuple(actions)

    def build(
        self, board: Blackboard, system: SystemName, limits: BudgetLimits, max_actions: int
    ) -> InvestigationReport:
        return self._assembler.assemble(
            board,
            system=system,
            built_by="deterministic",
            actions=self.actions(board, max_actions),
            limits=limits,
        )
