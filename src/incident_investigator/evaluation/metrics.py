"""Run scoring and aggregation (Req 26.9, 27). Pure functions: no LLM call, no I/O.

Scoring reads the Investigation_Report, the Run_Ledger (``BlackboardState``), the Run_Trace and the
Answer_Key. Repeated scoring of the same inputs gives identical results (Req 27.9).
"""

import statistics
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from typing import Protocol

from ..models.answer_key import AnswerKey, RootCauseLabel
from ..models.blackboard import BlackboardState
from ..models.claims import ClaimBody, ClaimDraft
from ..models.evidence import EvidenceItem
from ..models.report import InvestigationReport, ReportHypothesis
from ..models.results import MetricKey, MetricStat, ResultsRow, RunScore
from ..models.tools import ToolRecord
from ..models.trace import RunTrace
from ..models.vocab import SystemName, Verdict
from ..state.support import CitingStatement, HypothesisCitation, IncidentClaimCitation, SupportCheck

ALL_ROW = "ALL"
DECIMALS = 4
"""The Markdown Results_Table prints means and Spreads to this many decimals. The JSON keeps the exact
``statistics.mean`` / ``statistics.stdev`` values (design Property 62)."""

SYSTEM_ORDER: tuple[SystemName, ...] = (SystemName.MULTI, SystemName.BASELINE)


class SupportChecker(Protocol):
    def supports(self, stmt: CitingStatement, e: EvidenceItem, records: Sequence[ToolRecord]) -> bool: ...


@dataclass(frozen=True)
class RunIdentity:
    scenario: str
    system: SystemName
    run_index: int
    seed: int


# Match and accuracy rules (Req 27.1-27.3, 27.13-27.16)


def matches(hypothesis: ReportHypothesis, label: RootCauseLabel) -> bool:
    """Component equal after trim + lowercase, Cause_Type exactly equal; nothing else counts (Req 27.3)."""
    return (
        hypothesis.component.strip().lower() == label.component.strip().lower()
        and hypothesis.cause_type == label.cause_type
    )


def _top(report: InvestigationReport, n: int) -> list[ReportHypothesis]:
    return sorted(report.hypotheses, key=lambda h: h.rank)[:n]


def top_k_score(report: InvestigationReport, key: AnswerKey, k: int) -> int:
    """1 for an inconclusive Verdict on an insufficient-evidence key, or a conclusive Verdict with a match in the top k."""
    if key.root_cause is None:
        return int(report.verdict is Verdict.INCONCLUSIVE)
    if report.verdict is not Verdict.CONCLUSIVE:
        return 0
    return int(any(matches(h, key.root_cause) for h in _top(report, k)))


def hit_at_3(report: InvestigationReport, key: AnswerKey) -> int | None:
    """A match in the top 3 regardless of Verdict; not applicable for an insufficient-evidence key."""
    if key.root_cause is None:
        return None
    return int(any(matches(h, key.root_cause) for h in _top(report, 3)))


def over_cautious(report: InvestigationReport, key: AnswerKey) -> int:
    if key.root_cause is None or report.verdict is not Verdict.INCONCLUSIVE:
        return 0
    rank1 = _top(report, 1)
    return int(bool(rank1) and matches(rank1[0], key.root_cause))


def over_confident(report: InvestigationReport, key: AnswerKey) -> int:
    if report.verdict is not Verdict.CONCLUSIVE:
        return 0
    if key.root_cause is None:
        return 1
    rank1 = _top(report, 1)
    return int(not rank1 or not matches(rank1[0], key.root_cause))


# Grounding (Req 27.4, 27.5, 27.10-27.12)


def citations(report: InvestigationReport, ledger: BlackboardState) -> dict[tuple[str, str], CitingStatement | None]:
    """Every distinct (citing statement, Evidence_ID) pair outside the Evidence appendix.

    The statement is the ``state.support`` form the Support_Check judges. ``None`` marks a
    Recommended_Action whose Hypothesis is not in the report: it counts as a citation that is
    never supported.
    """
    pairs: dict[tuple[str, str], CitingStatement | None] = {}

    def add(statement_id: str, evidence_ids: Iterable[str], stmt: CitingStatement | None) -> None:
        for evidence_id in evidence_ids:
            pairs.setdefault((statement_id, evidence_id), stmt)

    hypotheses = (*report.hypotheses, *report.superseded_hypotheses)
    for h in hypotheses:
        add(f"hypothesis:{h.hypothesis_id}", h.supporting_evidence_ids, HypothesisCitation(h.component))
        add(f"hypothesis:{h.hypothesis_id}", h.contradicting_evidence_ids, HypothesisCitation(h.component, True))
    bodies: dict[str, ClaimBody] = {c.claim_id: c.body for c in ledger.findings}
    for fact in report.facts:
        # Reporting_Agent summary claims live only in the report: judge them as free-text claims.
        body = bodies.get(fact.claim_id) or ClaimDraft.model_construct(text=fact.text, evidence_ids=fact.evidence_ids)
        add(f"fact:{fact.claim_id}", fact.evidence_ids, body)
    components = {h.hypothesis_id: h.component for h in hypotheses}
    for action in report.recommended_actions:
        # A Recommended_Action citation is judged through its Hypothesis's Component (design D2).
        component = components.get(action.hypothesis_id)
        add(
            f"action:{action.action_id}",
            action.evidence_ids,
            HypothesisCitation(component) if component is not None else None,
        )
    incident = ledger.incident_report
    for claim in report.claim_statuses:
        for ids, contradicting in ((claim.corroborating_ids, False), (claim.contradicting_ids, True)):
            add(
                f"incident_claim:{claim.claim_id}",
                ids,
                IncidentClaimCitation(claim.kind, incident.affected_service, incident.start_time, contradicting),
            )
    return pairs


def citation_precision(report: InvestigationReport, ledger: BlackboardState, support: SupportChecker) -> float:
    """V / C over distinct citation pairs; 0.0 when the report cites nothing (Req 27.4, 27.10)."""
    pairs = citations(report, ledger)
    if not pairs:
        return 0.0
    evidence = {e.evidence_id: e for e in ledger.evidence}
    retained = {r.evidence_id: r.records for r in ledger.retained}
    valid = 0
    for (_, evidence_id), stmt in pairs.items():
        item = evidence.get(evidence_id)
        if stmt is not None and item is not None and support.supports(stmt, item, retained.get(evidence_id, ())):
            valid += 1
    return valid / len(pairs)


def key_evidence_recall(ledger: BlackboardState, key: AnswerKey) -> float | None:
    """F / K: Key_Evidence record IDs among any Evidence_Item's source record IDs; n/a when K = 0 (Req 27.5, 27.11)."""
    wanted = set(key.key_evidence)
    if not wanted:
        return None
    found = {rid for e in ledger.evidence for rid in e.source_record_ids}
    return len(wanted & found) / len(wanted)


def expected_gap_name(source: str, reason: str) -> str:
    return f"{source}:{reason}"


def expected_gaps_found(report: InvestigationReport | None, key: AnswerKey) -> dict[str, bool]:
    """Per expected Data_Gap: is a gap with the same source and reason listed? (Req 27.7)."""
    listed = {(str(g.source), g.reason) for g in report.data_gaps} if report is not None else set()
    return {
        expected_gap_name(str(g.source), g.reason): (str(g.source), g.reason) in listed for g in key.expected_data_gaps
    }


@dataclass(frozen=True)
class TraceCounts:
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def trace_counts(trace: RunTrace) -> TraceCounts:
    """Tool_Calls (each retry attempt separately) and provider token usage of every LLM call (Req 27.6)."""
    llm = trace.of_type("llm_call")
    return TraceCounts(
        tool_calls=len(trace.of_type("tool_call")),
        input_tokens=sum(e.input_tokens for e in llm),
        output_tokens=sum(e.output_tokens for e in llm),
    )


# Run scores


def score_run(
    run: RunIdentity,
    report: InvestigationReport,
    ledger: BlackboardState,
    trace: RunTrace,
    key: AnswerKey,
    *,
    latency_s: float,
    support: SupportChecker | None = None,
    isolation_violations: Sequence[str] = (),
) -> RunScore:
    checker = support if support is not None else SupportCheck()
    counts = trace_counts(trace)
    return RunScore(
        scenario=run.scenario,
        system=run.system,
        run_index=run.run_index,
        seed=run.seed,
        failed=False,
        top1=top_k_score(report, key, 1),
        top3=top_k_score(report, key, 3),
        hit_at_3=hit_at_3(report, key),
        over_cautious=over_cautious(report, key),
        over_confident=over_confident(report, key),
        citation_precision=citation_precision(report, ledger, checker),
        key_evidence_recall=key_evidence_recall(ledger, key),
        tool_calls=counts.tool_calls,
        latency_s=max(0.0, latency_s),
        input_tokens=counts.input_tokens,
        output_tokens=counts.output_tokens,
        total_tokens=counts.total_tokens,
        expected_gaps_found=expected_gaps_found(report, key),
        isolation_violations=tuple(isolation_violations),
    )


def failed_run_score(
    run: RunIdentity,
    key: AnswerKey | None,
    error: str,
    *,
    latency_s: float,
    counts: TraceCounts = TraceCounts(),
    isolation_violations: Sequence[str] = (),
) -> RunScore:
    """A failed Run: top-1/top-3 incorrect, precision 0.0, recall 0.0 when K > 0, every expected gap absent (Req 26.7, 27.12)."""
    has_key_evidence = key is not None and bool(key.key_evidence)
    return RunScore(
        scenario=run.scenario,
        system=run.system,
        run_index=run.run_index,
        seed=run.seed,
        failed=True,
        error=error,
        top1=0,
        top3=0,
        hit_at_3=None if key is not None and key.root_cause is None else 0,
        over_cautious=None,
        over_confident=None,
        citation_precision=0.0,
        key_evidence_recall=0.0 if has_key_evidence else None,
        tool_calls=counts.tool_calls,
        latency_s=max(0.0, latency_s),
        input_tokens=counts.input_tokens,
        output_tokens=counts.output_tokens,
        total_tokens=counts.total_tokens,
        expected_gaps_found=expected_gaps_found(None, key) if key is not None else {},
        isolation_violations=tuple(isolation_violations),
    )


# Aggregation (Req 26.9, 27.8, 27.17)

_VALUE: dict[MetricKey, Callable[[RunScore], float | int | None]] = {
    MetricKey.TOP1_ACCURACY: lambda s: s.top1,
    MetricKey.TOP3_ACCURACY: lambda s: s.top3,
    MetricKey.CITATION_PRECISION: lambda s: s.citation_precision,
    MetricKey.KEY_EVIDENCE_RECALL: lambda s: s.key_evidence_recall,
    MetricKey.TOOL_CALLS: lambda s: s.tool_calls,
    MetricKey.LATENCY_S: lambda s: s.latency_s,
    MetricKey.INPUT_TOKENS: lambda s: s.input_tokens,
    MetricKey.OUTPUT_TOKENS: lambda s: s.output_tokens,
    MetricKey.TOTAL_TOKENS: lambda s: s.total_tokens,
    MetricKey.RANKING_HIT_AT_3: lambda s: s.hit_at_3,
    MetricKey.OVER_CAUTIOUS_RATE: lambda s: s.over_cautious,
    MetricKey.OVER_CONFIDENT_RATE: lambda s: s.over_confident,
}
INCLUDES_FAILED_RUNS: frozenset[MetricKey] = frozenset(
    {MetricKey.TOP1_ACCURACY, MetricKey.TOP3_ACCURACY, MetricKey.RANKING_HIT_AT_3}
)
"""Failed Runs count in these means (as 0); every other metric excludes them (Req 26.9)."""


def metric_stat(scores: Sequence[RunScore], metric: MetricKey) -> MetricStat:
    value_of = _VALUE[metric]
    included = [s for s in scores if metric in INCLUDES_FAILED_RUNS or not s.failed]
    values = [float(v) for s in included if (v := value_of(s)) is not None]
    if values:
        mean = statistics.mean(values)
        spread = statistics.stdev(values) if len(values) > 1 else 0.0
        return MetricStat(mean=mean, spread=spread, n=len(values), status="ok")
    if all(value_of(s) is None for s in scores):
        # hit@3 on an insufficient-evidence key, recall with no Key_Evidence (Req 27.11, 27.14)
        if metric in (MetricKey.RANKING_HIT_AT_3, MetricKey.KEY_EVIDENCE_RECALL):
            return MetricStat(mean=None, spread=None, n=0, status="not_applicable")
    return MetricStat(mean=None, spread=None, n=0, status="unavailable")  # every Run failed


def _row(scenario: str, system: SystemName, scores: Sequence[RunScore]) -> ResultsRow:
    return ResultsRow(
        scenario=scenario,
        system=system,
        runs=len(scores),
        failed_runs=sum(1 for s in scores if s.failed),
        metrics={metric: metric_stat(scores, metric) for metric in MetricKey},
    )


def _systems(scores: Sequence[RunScore]) -> list[SystemName]:
    present = {s.system for s in scores}
    return [system for system in SYSTEM_ORDER if system in present]


def aggregate(scores: Sequence[RunScore]) -> list[ResultsRow]:
    """One row per scenario x system, then one pooled "ALL" row per system."""
    rows = []
    for scenario in sorted({s.scenario for s in scores}):
        for system in _systems(scores):
            cell = [s for s in scores if s.scenario == scenario and s.system == system]
            if cell:
                rows.append(_row(scenario, system, cell))
    for system in _systems(scores):
        rows.append(_row(ALL_ROW, system, [s for s in scores if s.system == system]))
    return rows
