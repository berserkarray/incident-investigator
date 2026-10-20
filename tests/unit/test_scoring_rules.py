"""Evaluator scoring rules and aggregation (Req 26.9, 27.1-27.17; design "Evaluator", Properties 59-62).

Written against the design interfaces in ``evaluation/metrics.py``::

    score_run(report, ledger, trace, key, support) -> RunScore        # pure; report None = failed Run
    aggregate(scores) -> list[ResultsRow]                              # per scenario x system, plus "ALL"

``score()`` and ``aggregate()`` below are the only call sites, so a signature that grows extra
arguments at integration changes one line each. The Support_Check is stubbed: it passes only the
Evidence_IDs it is given, so citation precision is checked independently of the D2 rule table.
"""

import statistics

import pytest
from factories import at, make_incident_report, window

from incident_investigator.clock import FakeClock
from incident_investigator.models.answer_key import AnswerKey, ExpectedDataGap, RootCauseLabel
from incident_investigator.models.budget import TokenCount
from incident_investigator.models.evidence import ObservationSummary
from incident_investigator.models.gaps import DataGap
from incident_investigator.models.report import (
    BudgetUsageReport,
    FactEntry,
    IncidentReference,
    InvestigationReport,
    ReportHypothesis,
    UsageLine,
)
from incident_investigator.models.results import MetricKey, RunScore
from incident_investigator.models.snapshot import ChangeEvent, LogRecord
from incident_investigator.models.tools import GetDeploymentsInput, QueryLogsInput
from incident_investigator.models.vocab import (
    AgentName,
    CallKind,
    ChangeType,
    LogLevel,
    Reliability,
    SystemName,
    TerminationReason,
    ToolName,
    Verdict,
)
from incident_investigator.state.blackboard import Blackboard
from incident_investigator.tracing import TraceRecorder

pytestmark = pytest.mark.skip(reason="needs ws/eval-baseline")

C, I = Verdict.CONCLUSIVE, Verdict.INCONCLUSIVE
ROOT = "checkout-a1b"
CANARY = "CANARY-" + "0" * 48


# Interfaces under test


def score(report, ledger, trace, key, support=None) -> RunScore:
    from incident_investigator.evaluation.metrics import score_run

    return score_run(report, ledger, trace, key, support or SupportOnly({"E0001"}))


def aggregate(scores):
    from incident_investigator.evaluation import metrics

    return metrics.aggregate(scores)


class SupportOnly:
    """Support_Check stub: an Evidence_Item supports any statement iff its ID is in ``ids``."""

    def __init__(self, ids):
        self.ids = frozenset(ids)

    def supports(self, statement, evidence, records=()) -> bool:
        return evidence.evidence_id in self.ids


# Builders


def label_key(**overrides) -> AnswerKey:
    data = dict(
        incident_id="inc-test0001",
        scenario_name="scenario",
        seed=42,
        outcome="root_cause",
        root_cause=RootCauseLabel(component=ROOT, cause_type="config_error"),
        key_evidence=("log-2", "chg-1", "met-9"),
        canary=CANARY,
    )
    return AnswerKey(**{**data, **overrides})


def insufficient_key(**overrides) -> AnswerKey:
    data = dict(incident_id="inc-test0001", scenario_name="scenario", seed=42, outcome="insufficient_evidence", canary=CANARY)
    return AnswerKey(**{**data, **overrides})


def hyp(rank: int, component: str = ROOT, cause_type: str = "config_error", supporting=()) -> ReportHypothesis:
    confidence = round(0.9 - 0.1 * rank, 2)
    return ReportHypothesis(
        hypothesis_id=f"H{rank}",
        rank=rank,
        revision=1,
        component=component,
        cause_type=cause_type,
        statement="statement",
        supporting_evidence_ids=tuple(supporting),
        confidence=confidence,
        raw_confidence=confidence,
        evidence_sufficient=True,
    )


def ranked(*components: str) -> tuple[ReportHypothesis, ...]:
    """Hypotheses ranked 1..N; ``"*"`` stands for the key label, anything else is a wrong Component."""
    return tuple(hyp(rank, ROOT if c == "*" else c) for rank, c in enumerate(components, start=1))


def make_report(verdict: Verdict, hypotheses=(), *, facts=(), data_gaps=()) -> InvestigationReport:
    line = UsageLine(used=0, limit=10)
    return InvestigationReport(
        system=SystemName.MULTI,
        built_by="deterministic",
        incident=IncidentReference(
            incident_id="inc-test0001", title="t", affected_service=ROOT, start_time=at(60), severity="sev2"
        ),
        verdict=verdict,
        termination_reason=TerminationReason.CRITIC_ACCEPTED if verdict is C else TerminationReason.MAX_CRITIC_ROUNDS,
        hypotheses=tuple(hypotheses),
        facts=tuple(facts),
        data_gaps=tuple(data_gaps),
        budget_usage=BudgetUsageReport(
            tool_calls=line, tokens=line, input_tokens=0, output_tokens=0, critic_rounds=line, wall_clock_s=line
        ),
    )


def gap(source: ToolName, reason: str) -> DataGap:
    return DataGap(source=source, reason=reason, gap_id="G001", recorded_at=at(0))


@pytest.fixture
async def ledger():
    """E0001: query_logs returning log-2; E0002: get_deployments returning chg-1."""
    board = Blackboard("run", "inc-test0001", make_incident_report(), FakeClock())
    log = LogRecord(record_id="log-2", timestamp=at(61), service=ROOT, level=LogLevel.ERROR, message="timeout 504")
    change = ChangeEvent(
        record_id="chg-1", timestamp=at(50), service=ROOT, change_type=ChangeType.CONFIG_CHANGE, summary="pool 50 -> 5"
    )
    for tool, query, record in (
        (ToolName.QUERY_LOGS, QueryLogsInput(service=ROOT, time_range=window(0, 120), limit=25), log),
        (ToolName.GET_DEPLOYMENTS, GetDeploymentsInput(service=ROOT, time_range=window(0, 60), limit=25), change),
    ):
        await board.append_evidence(
            tool=tool,
            query=query,
            summary=ObservationSummary(record_count=1),
            time_range=query.time_range,
            reliability=Reliability.COMPLETE,
            records=(record,),
            produced_by=AgentName.OBSERVABILITY,
            task_id="T1",
        )
    for _ in range(3):
        board.reserve_tool_call(60)
    await board.add_usage(AgentName.REASONING, TokenCount(input_tokens=100, output_tokens=20))
    await board.add_usage(AgentName.CRITIC, TokenCount(input_tokens=50, output_tokens=10))
    return board.to_state()


@pytest.fixture
def trace():
    """Three Tool_Calls (one retried request plus one other) and two LLM calls (150 in, 30 out)."""
    recorder = TraceRecorder("run", SystemName.MULTI, FakeClock())
    logs = {"tool": "query_logs", "service": ROOT, "time_range": window(0, 120).model_dump(mode="json"), "limit": 25}
    for attempt, error in ((1, "timeout"), (2, None)):
        recorder.record(
            "tool_call", caller=AgentName.OBSERVABILITY, task_id="T1", tool=ToolName.QUERY_LOGS, input=logs,
            attempt=attempt, error_code=error, duration_s=0.0,
        )
    recorder.record(
        "tool_call", caller=AgentName.DEPENDENCY, task_id="T2", tool=ToolName.GET_DEPLOYMENTS,
        input={"tool": "get_deployments", "time_range": window(0, 60).model_dump(mode="json"), "limit": 25},
        attempt=1, duration_s=0.0,
    )
    for caller, kind, tokens_in, tokens_out in ((AgentName.REASONING, CallKind.REASONING, 100, 20), (AgentName.CRITIC, CallKind.CRITIC, 50, 10)):
        recorder.record(
            "llm_call", caller=caller, call_kind=kind, attempt=1, input_tokens=tokens_in, output_tokens=tokens_out,
            duration_s=0.0, outcome="ok",
        )
    return recorder.to_trace()


# Top-1 and top-3 (Req 27.1, 27.2, 30.2: all four key outcome x Verdict combinations)


@pytest.mark.parametrize(
    ("key_kind", "verdict", "top1", "top3"),
    [("label", C, 1, 1), ("label", I, 0, 0), ("insufficient", I, 1, 1), ("insufficient", C, 0, 0)],
)
def test_top1_top3_for_each_key_outcome_and_verdict(ledger, trace, key_kind, verdict, top1, top3):
    key = label_key() if key_kind == "label" else insufficient_key()
    result = score(make_report(verdict, ranked("*", "other-1", "other-2")), ledger, trace, key)
    assert (result.failed, result.top1, result.top3) == (False, top1, top3)


@pytest.mark.parametrize("component", [" Checkout-A1B ", "CHECKOUT-A1B", "checkout-a1b\t", "\ncheckout-a1b"])
def test_component_match_ignores_case_and_surrounding_whitespace(ledger, trace, component):
    result = score(make_report(C, (hyp(1, component),)), ledger, trace, label_key())
    assert result.top1 == 1 and result.top3 == 1


@pytest.mark.parametrize(
    "hypothesis",
    [hyp(1, ROOT, "deploy_regression"), hyp(1, "checkout-a1"), hyp(1, "checkout a1b")],
    ids=["cause-type-differs", "component-prefix", "inner-whitespace"],
)
def test_a_match_needs_the_exact_cause_type_and_the_same_component(ledger, trace, hypothesis):
    result = score(make_report(C, (hypothesis,)), ledger, trace, label_key())
    assert (result.top1, result.top3, result.hit_at_3) == (0, 0, 0)


@pytest.mark.parametrize(
    ("components", "top1", "top3"),
    [
        (("*",), 1, 1),
        (("x-1", "*"), 0, 1),  # fewer than 3: only the ranks present count
        (("x-1", "x-2", "*"), 0, 1),
        (("x-1", "x-2", "x-3", "*"), 0, 0),  # rank 4 is outside the top 3
        (("x-1",), 0, 0),
        ((), 0, 0),  # no Hypotheses
    ],
)
def test_top3_considers_ranks_one_to_three_only(ledger, trace, components, top1, top3):
    result = score(make_report(C, ranked(*components)), ledger, trace, label_key())
    assert (result.top1, result.top3) == (top1, top3)


# Ranking hit@3 (Req 27.13, 27.14)


@pytest.mark.parametrize("verdict", [C, I])
@pytest.mark.parametrize(("components", "hit"), [(("x-1", "x-2", "*"), 1), (("x-1", "x-2", "x-3", "*"), 0), ((), 0)])
def test_hit_at_3_ignores_the_verdict(ledger, trace, verdict, components, hit):
    assert score(make_report(verdict, ranked(*components)), ledger, trace, label_key()).hit_at_3 == hit


@pytest.mark.parametrize("verdict", [C, I])
def test_hit_at_3_is_not_applicable_for_the_insufficient_marker(ledger, trace, verdict):
    assert score(make_report(verdict, ranked("*")), ledger, trace, insufficient_key()).hit_at_3 is None


# Over-cautious and over-confident indicators (Req 27.15, 27.16)


@pytest.mark.parametrize(
    ("key_kind", "verdict", "components", "over_cautious", "over_confident"),
    [
        ("label", I, ("*",), 1, 0),  # right answer, not committed to
        ("label", I, ("x-1", "*"), 0, 0),
        ("label", C, ("*",), 0, 0),
        ("label", C, ("x-1", "*"), 0, 1),  # committed to a wrong rank 1
        ("insufficient", C, ("*",), 0, 1),  # committed when the evidence is insufficient
        ("insufficient", I, ("*",), 0, 0),
    ],
)
def test_diagnostic_indicators(ledger, trace, key_kind, verdict, components, over_cautious, over_confident):
    key = label_key() if key_kind == "label" else insufficient_key()
    result = score(make_report(verdict, ranked(*components)), ledger, trace, key)
    assert (result.over_cautious, result.over_confident) == (over_cautious, over_confident)


# Failed Runs (Req 26.7, 27.1, 27.2, 27.12, 27.13)


def test_a_failed_run_scores_zero_everywhere():
    key = label_key(expected_data_gaps=(ExpectedDataGap(source=ToolName.QUERY_LOGS, reason="timeout"),))
    result = score(None, None, None, key)
    assert result.failed
    assert (result.top1, result.top3, result.hit_at_3) == (0, 0, 0)
    assert result.citation_precision == 0.0 and result.key_evidence_recall == 0.0
    assert list(result.expected_gaps_found.values()) == [False]


def test_a_failed_run_against_the_insufficient_marker_is_still_incorrect():
    result = score(None, None, None, insufficient_key())
    assert result.failed and (result.top1, result.top3) == (0, 0)
    assert result.hit_at_3 is None and result.key_evidence_recall is None


# Citation precision and Key_Evidence recall (Req 27.4, 27.5, 27.10, 27.11)


def test_citation_precision_is_supported_distinct_pairs_over_all_pairs(ledger, trace):
    # pairs: (F001, E0001) once despite the repeat, (H1, E0001), (H1, E0002), (H2, E0001), (H2, E0099) -> C = 5
    # supported: E0001 only, E0002 fails the check, E0099 is not in the ledger -> V = 3
    facts = (FactEntry(claim_id="F001", producer=AgentName.OBSERVABILITY, text="errors", evidence_ids=("E0001", "E0001")),)
    hypotheses = (hyp(1, supporting=("E0001", "E0002")), hyp(2, "x-1", supporting=("E0001", "E0099")))
    result = score(make_report(C, hypotheses, facts=facts), ledger, trace, label_key())
    assert result.citation_precision == pytest.approx(3 / 5)


def test_citation_precision_is_zero_without_citations(ledger, trace):
    result = score(make_report(I, ranked("*", "x-1")), ledger, trace, label_key())
    assert result.citation_precision == 0.0


def test_key_evidence_recall_counts_key_ids_found_in_any_evidence_item(ledger, trace):
    # log-2 (E0001) and chg-1 (E0002) are in the ledger, met-9 is not; the report cites nothing
    result = score(make_report(I, ranked("*", "x-1")), ledger, trace, label_key())
    assert result.key_evidence_recall == pytest.approx(2 / 3)


def test_key_evidence_recall_is_not_applicable_without_key_evidence(ledger, trace):
    assert score(make_report(I), ledger, trace, insufficient_key()).key_evidence_recall is None


# Expected Data_Gaps, Tool_Calls and tokens (Req 27.6, 27.7)


@pytest.mark.parametrize(
    ("gaps", "found"),
    [
        ((gap(ToolName.QUERY_LOGS, "timeout"),), True),
        ((gap(ToolName.QUERY_LOGS, "budget_exhausted"),), False),  # same source, other reason
        ((gap(ToolName.QUERY_METRICS, "timeout"),), False),  # same reason, other source
        ((), False),
    ],
)
def test_expected_data_gap_needs_the_same_source_and_reason(ledger, trace, gaps, found):
    key = label_key(expected_data_gaps=(ExpectedDataGap(source=ToolName.QUERY_LOGS, reason="timeout"),))
    result = score(make_report(I, data_gaps=gaps), ledger, trace, key)
    assert list(result.expected_gaps_found.values()) == [found]


def test_tool_calls_count_every_attempt_and_tokens_sum_every_call(ledger, trace):
    result = score(make_report(I), ledger, trace, label_key())
    assert result.tool_calls == 3
    assert (result.input_tokens, result.output_tokens, result.total_tokens) == (150, 30, 180)


def test_scoring_is_deterministic(ledger, trace):
    report = make_report(C, (hyp(1, supporting=("E0001", "E0002")), hyp(2, "x-1")))
    assert score(report, ledger, trace, label_key()) == score(report, ledger, trace, label_key())


# Aggregation (Req 26.9, 27.8, 27.17)


def run(scenario, system=SystemName.MULTI, index=1, *, failed=False, **metrics) -> RunScore:
    defaults = dict(top1=0, top3=0)
    if not failed:
        defaults.update(over_cautious=0, over_confident=0, citation_precision=0.0)
    return RunScore(scenario=scenario, system=system, run_index=index, seed=index, failed=failed, **{**defaults, **metrics})


SCORES = [
    # alpha: two good Runs and one failure
    run("alpha", index=1, top1=1, top3=1, hit_at_3=1, citation_precision=0.5, key_evidence_recall=0.5, tool_calls=10, latency_s=2.0, total_tokens=100),
    run("alpha", index=2, top1=0, top3=1, hit_at_3=1, over_cautious=1, citation_precision=1.0, key_evidence_recall=1.0, tool_calls=20, latency_s=4.0, total_tokens=300),
    run("alpha", index=3, failed=True, hit_at_3=0, citation_precision=0.0, key_evidence_recall=0.0, tool_calls=5, latency_s=1.0),
    # beta: every Run failed
    run("beta", index=1, failed=True, hit_at_3=0, citation_precision=0.0, key_evidence_recall=0.0),
    run("beta", index=2, failed=True, hit_at_3=0, citation_precision=0.0, key_evidence_recall=0.0),
    # gamma: insufficient-evidence key, so hit@3 and recall are not applicable
    run("gamma", index=1, top1=1, top3=1, citation_precision=0.25, tool_calls=8, latency_s=3.0),
    # one baseline Run
    run("alpha", SystemName.BASELINE, 1, top1=1, top3=1, hit_at_3=1, over_confident=1, citation_precision=0.75, key_evidence_recall=1.0, tool_calls=4),
]


def row(rows, scenario, system=SystemName.MULTI):
    (match,) = [r for r in rows if r.scenario == scenario and r.system == system]
    return match


def check(stat, values):
    assert stat.status == "ok" and stat.n == len(values)
    assert stat.mean == pytest.approx(statistics.mean(values))
    assert stat.spread == pytest.approx(statistics.stdev(values) if len(values) > 1 else 0.0)


def test_failed_runs_count_in_accuracy_but_not_in_the_other_metrics():
    alpha = row(aggregate(SCORES), "alpha")
    assert (alpha.runs, alpha.failed_runs) == (3, 1)
    check(alpha.metrics[MetricKey.TOP1_ACCURACY], [1, 0, 0])
    check(alpha.metrics[MetricKey.TOP3_ACCURACY], [1, 1, 0])
    check(alpha.metrics[MetricKey.RANKING_HIT_AT_3], [1, 1, 0])
    check(alpha.metrics[MetricKey.CITATION_PRECISION], [0.5, 1.0])
    check(alpha.metrics[MetricKey.KEY_EVIDENCE_RECALL], [0.5, 1.0])
    check(alpha.metrics[MetricKey.TOOL_CALLS], [10, 20])
    check(alpha.metrics[MetricKey.LATENCY_S], [2.0, 4.0])
    check(alpha.metrics[MetricKey.TOTAL_TOKENS], [100, 300])
    check(alpha.metrics[MetricKey.OVER_CAUTIOUS_RATE], [0, 1])
    check(alpha.metrics[MetricKey.OVER_CONFIDENT_RATE], [0, 0])


def test_an_all_failed_cell_reports_excluded_metrics_as_unavailable():
    beta = row(aggregate(SCORES), "beta")
    assert (beta.runs, beta.failed_runs) == (2, 2)
    check(beta.metrics[MetricKey.TOP1_ACCURACY], [0, 0])
    for key in (MetricKey.CITATION_PRECISION, MetricKey.TOOL_CALLS, MetricKey.LATENCY_S, MetricKey.OVER_CAUTIOUS_RATE):
        assert beta.metrics[key].status == "unavailable" and beta.metrics[key].mean is None, key


def test_not_applicable_cells_and_single_run_spread():
    rows = aggregate(SCORES)
    gamma = row(rows, "gamma")
    assert gamma.metrics[MetricKey.RANKING_HIT_AT_3].status == "not_applicable"
    assert gamma.metrics[MetricKey.KEY_EVIDENCE_RECALL].status == "not_applicable"
    check(gamma.metrics[MetricKey.TOP1_ACCURACY], [1])  # one value: spread 0.0
    check(row(rows, "alpha", SystemName.BASELINE).metrics[MetricKey.OVER_CONFIDENT_RATE], [1])


def test_all_rows_pool_per_run_values_across_scenarios():
    rows = aggregate(SCORES)
    assert {(r.scenario, r.system) for r in rows} == {
        ("alpha", SystemName.MULTI),
        ("beta", SystemName.MULTI),
        ("gamma", SystemName.MULTI),
        ("alpha", SystemName.BASELINE),
        ("ALL", SystemName.MULTI),
        ("ALL", SystemName.BASELINE),
    }
    pooled = row(rows, "ALL")
    assert (pooled.runs, pooled.failed_runs) == (6, 3)
    check(pooled.metrics[MetricKey.TOP1_ACCURACY], [1, 0, 0, 0, 0, 1])
    check(pooled.metrics[MetricKey.RANKING_HIT_AT_3], [1, 1, 0, 0, 0])  # gamma is not applicable
    check(pooled.metrics[MetricKey.CITATION_PRECISION], [0.5, 1.0, 0.25])
    check(pooled.metrics[MetricKey.TOOL_CALLS], [10, 20, 8])
    check(pooled.metrics[MetricKey.OVER_CAUTIOUS_RATE], [0, 1, 0])
