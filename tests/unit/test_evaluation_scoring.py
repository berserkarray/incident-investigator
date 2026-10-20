"""Evaluator scoring rules, aggregation and the Results_Table (task 8.6).

Req 26.2, 26.5, 26.9, 27.1-27.5, 27.7, 27.9-27.17.
"""

import json
import statistics
from datetime import date

import pytest
from factories import at, make_incident_report, window

from incident_investigator.evaluation.metrics import (
    ALL_ROW,
    RunIdentity,
    aggregate,
    citation_precision,
    citations,
    failed_run_score,
    hit_at_3,
    key_evidence_recall,
    matches,
    metric_stat,
    over_cautious,
    over_confident,
    score_run,
    top_k_score,
)
from incident_investigator.evaluation.results import format_stat, render_results_markdown
from incident_investigator.evaluation.runner import run_seed
from incident_investigator.state.support import SupportCheck
from incident_investigator.models.answer_key import AnswerKey, ExpectedDataGap, RootCauseLabel
from incident_investigator.models.blackboard import BlackboardState
from incident_investigator.models.claims import LabelledClaim, LogSignal, ObservabilityClaim
from incident_investigator.models.evidence import EvidenceItem, ObservationSummary, RetainedRecords
from incident_investigator.models.gaps import DataGap
from incident_investigator.models.incident import IncidentClaim, IncidentClaimKind
from incident_investigator.models.report import (
    BudgetUsageReport,
    FactEntry,
    IncidentReference,
    InvestigationReport,
    RecommendedAction,
    ReportHypothesis,
    UsageLine,
)
from incident_investigator.models.results import MetricKey, ResultsTable, RunScore
from incident_investigator.models.snapshot import LogRecord, PastIncident
from incident_investigator.models.tools import QueryLogsInput, SearchIncidentsInput
from incident_investigator.models.trace import RunTrace
from incident_investigator.models.vocab import (
    AgentName,
    CauseType,
    LogLevel,
    Reliability,
    SystemName,
    TerminationReason,
    ToolName,
    Verdict,
)

CANARY = "CANARY-" + "a" * 48
LABEL = RootCauseLabel(component="checkout-a1b", cause_type=CauseType.CONFIG_ERROR)
RUN = RunIdentity("bad-config-deploy", SystemName.MULTI, 1, 7)


def label_key(**overrides) -> AnswerKey:
    data = dict(
        incident_id="inc-1",
        scenario_name="bad-config-deploy",
        seed=42,
        outcome="root_cause",
        root_cause=LABEL,
        key_evidence=("log-2", "chg-1"),
        canary=CANARY,
    )
    data.update(overrides)
    return AnswerKey(**data)


def insufficient_key(**overrides) -> AnswerKey:
    data = dict(
        incident_id="inc-2",
        scenario_name="insufficient-evidence",
        seed=42,
        outcome="insufficient_evidence",
        root_cause=None,
        canary=CANARY,
    )
    data.update(overrides)
    return AnswerKey(**data)


def hyp(rank: int, component: str = "checkout-a1b", cause=CauseType.CONFIG_ERROR, support=(), contra=(), hid=None):
    confidence = round(0.9 - 0.1 * rank, 2)
    return ReportHypothesis(
        hypothesis_id=hid or f"H{rank}",
        rank=rank,
        revision=1,
        component=component,
        cause_type=cause,
        statement="s",
        supporting_evidence_ids=support,
        contradicting_evidence_ids=contra,
        confidence=confidence,
        raw_confidence=confidence,
        evidence_sufficient=True,
    )


def make_report(verdict: Verdict, hypotheses=(), **extra) -> InvestigationReport:
    line = UsageLine(used=0, limit=1)
    incident = make_incident_report()
    return InvestigationReport(
        system=SystemName.MULTI,
        built_by="deterministic",
        incident=IncidentReference(
            incident_id="inc-1",
            title=incident.title,
            affected_service=incident.affected_service,
            start_time=incident.start_time,
            severity=incident.severity,
        ),
        verdict=verdict,
        termination_reason=TerminationReason.CRITIC_ACCEPTED,
        hypotheses=tuple(hypotheses),
        budget_usage=BudgetUsageReport(
            tool_calls=line, tokens=line, input_tokens=0, output_tokens=0, critic_rounds=line, wall_clock_s=line
        ),
        **extra,
    )


WRONG = hyp(1, "payments-c2d", CauseType.CONFIG_ERROR)
RIGHT = hyp(1)


# Top-1 and top-3: all four combinations of key outcome x Verdict (Req 27.1, 27.2)


@pytest.mark.parametrize(
    ("key", "verdict", "hypotheses", "top1", "top3"),
    [
        (label_key(), Verdict.CONCLUSIVE, [RIGHT], 1, 1),
        (label_key(), Verdict.INCONCLUSIVE, [RIGHT], 0, 0),  # correct label, but inconclusive
        (insufficient_key(), Verdict.INCONCLUSIVE, [RIGHT], 1, 1),
        (insufficient_key(), Verdict.CONCLUSIVE, [RIGHT], 0, 0),
        (insufficient_key(), Verdict.INCONCLUSIVE, [], 1, 1),
        (label_key(), Verdict.CONCLUSIVE, [WRONG], 0, 0),
    ],
)
def test_top1_and_top3_for_each_key_and_verdict(key, verdict, hypotheses, top1, top3):
    report = make_report(verdict, hypotheses)
    assert top_k_score(report, key, 1) == top1
    assert top_k_score(report, key, 3) == top3


def test_top3_counts_only_ranks_one_to_three():
    second = [hyp(1, "payments-c2d"), hyp(2)]
    third = [hyp(1, "payments-c2d"), hyp(2, "orders-db-456"), hyp(3)]
    fourth = [hyp(1, "payments-c2d"), hyp(2, "orders-db-456"), hyp(3, "lb-0aa"), hyp(4)]
    key = label_key()
    assert top_k_score(make_report(Verdict.CONCLUSIVE, second), key, 3) == 1  # fewer than 3 hypotheses
    assert top_k_score(make_report(Verdict.CONCLUSIVE, third), key, 3) == 1
    assert top_k_score(make_report(Verdict.CONCLUSIVE, fourth), key, 3) == 0
    assert top_k_score(make_report(Verdict.CONCLUSIVE, third), key, 1) == 0
    assert top_k_score(make_report(Verdict.CONCLUSIVE, []), key, 3) == 0  # no hypotheses


@pytest.mark.parametrize(
    ("component", "cause", "expected"),
    [
        ("checkout-a1b", CauseType.CONFIG_ERROR, True),
        ("  Checkout-A1B ", CauseType.CONFIG_ERROR, True),  # trimmed and case-folded (Req 27.3)
        ("checkout-a1b", CauseType.DEPLOY_REGRESSION, False),  # Cause_Type must match exactly
        ("checkout", CauseType.CONFIG_ERROR, False),
    ],
)
def test_match_rule(component, cause, expected):
    assert matches(hyp(1, component, cause), RootCauseLabel(component=" CHECKOUT-a1b", cause_type="config_error")) is expected


# Ranking hit@3 and the diagnostic indicators (Req 27.13-27.16)


def test_hit_at_3_ignores_the_verdict_and_is_na_for_insufficient_keys():
    ranked = [hyp(1, "payments-c2d"), hyp(2)]
    assert hit_at_3(make_report(Verdict.INCONCLUSIVE, ranked), label_key()) == 1
    assert hit_at_3(make_report(Verdict.CONCLUSIVE, ranked), label_key()) == 1
    assert hit_at_3(make_report(Verdict.CONCLUSIVE, []), label_key()) == 0
    assert hit_at_3(make_report(Verdict.INCONCLUSIVE, ranked), insufficient_key()) is None


@pytest.mark.parametrize(
    ("key", "verdict", "hypotheses", "cautious", "confident"),
    [
        (label_key(), Verdict.INCONCLUSIVE, [RIGHT], 1, 0),
        (label_key(), Verdict.INCONCLUSIVE, [WRONG], 0, 0),
        (label_key(), Verdict.CONCLUSIVE, [RIGHT], 0, 0),
        (label_key(), Verdict.CONCLUSIVE, [WRONG], 0, 1),
        (insufficient_key(), Verdict.CONCLUSIVE, [RIGHT], 0, 1),
        (insufficient_key(), Verdict.INCONCLUSIVE, [RIGHT], 0, 0),
    ],
)
def test_over_cautious_and_over_confident(key, verdict, hypotheses, cautious, confident):
    report = make_report(verdict, hypotheses)
    assert over_cautious(report, key) == cautious
    assert over_confident(report, key) == confident


def test_failed_runs_score_incorrect():
    failed = failed_run_score(RUN, label_key(expected_data_gaps=(ExpectedDataGap(source="query_logs", reason="timeout"),)), "boom", latency_s=2.0)
    assert (failed.top1, failed.top3, failed.hit_at_3) == (0, 0, 0)
    assert failed.citation_precision == 0.0 and failed.key_evidence_recall == 0.0  # Req 27.12
    assert failed.over_cautious is None and failed.over_confident is None
    assert failed.expected_gaps_found == {"query_logs:timeout": False}
    on_insufficient = failed_run_score(RUN, insufficient_key(), "boom", latency_s=0.0)
    assert on_insufficient.hit_at_3 is None and on_insufficient.key_evidence_recall is None
    assert on_insufficient.top1 == 0  # a failed Run is never correct, even on an insufficient key


# Citation precision and Key_Evidence recall (Req 27.4, 27.5, 27.10, 27.11)

LOGS_QUERY = QueryLogsInput(service="checkout-a1b", time_range=window(0, 120), limit=25)
ERROR_LOG = LogRecord(record_id="log-2", timestamp=at(61), service="checkout-a1b", level=LogLevel.ERROR, message="timeout 504")
PAST = PastIncident(
    record_id="pin-1",
    occurred_at=at(-10_000),
    title="Checkout outage",
    summary="pool exhausted",
    affected_services=("checkout-a1b",),
    symptoms=("5xx",),
    cause_type=CauseType.CONNECTION_POOL_EXHAUSTION,
    resolution="raised pool",
)


def evidence(evidence_id: str, seq: int, tool: ToolName, query, records) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        seq=seq,
        tool=tool,
        query=query,
        observation=ObservationSummary(record_count=len(records)),
        time_range=getattr(query, "time_range", None),
        reliability=Reliability.COMPLETE,
        source_record_ids=tuple(r.record_id for r in records),
        produced_by=AgentName.OBSERVABILITY,
        task_id="T1",
        recorded_at=at(0),
    )


def make_ledger(findings=()) -> BlackboardState:
    items = (
        evidence("E0001", 1, ToolName.QUERY_LOGS, LOGS_QUERY, (ERROR_LOG,)),
        evidence("E0002", 2, ToolName.SEARCH_INCIDENTS, SearchIncidentsInput(query="checkout", limit=25), (PAST,)),
    )
    return BlackboardState(
        run_id="r",
        incident_id="inc-1",
        incident_report=make_incident_report(),
        evidence=items,
        retained=(
            RetainedRecords(evidence_id="E0001", records=(ERROR_LOG,)),
            RetainedRecords(evidence_id="E0002", records=(PAST,)),
        ),
        findings=tuple(findings),
    )


def test_citation_precision_counts_distinct_pairs_that_pass_the_support_check():
    claim = ObservabilityClaim(
        text="checkout errors from 01:01",
        service="checkout-a1b",
        signal=LogSignal(level=LogLevel.ERROR, pattern="timeout"),
        onset=at(61),
        evidence_ids=("E0001",),
    )
    ledger = make_ledger([LabelledClaim(claim_id="F001", producer=AgentName.OBSERVABILITY, task_id="T1", body=claim, label="fact")])
    report = make_report(
        Verdict.CONCLUSIVE,
        # E0001 twice is one pair; E0002 is historical; E0404 is not in the ledger
        [hyp(1, support=("E0001", "E0001", "E0002", "E0404"))],
        facts=(FactEntry(claim_id="F001", producer=AgentName.OBSERVABILITY, text=claim.text, evidence_ids=("E0001",)),),
        recommended_actions=(
            RecommendedAction(action_id="A1", text="roll back", category="mitigation", hypothesis_id="H1", evidence_ids=("E0001",), producer="deterministic"),
        ),
        claim_statuses=(
            IncidentClaim(claim_id="IC1", kind=IncidentClaimKind.SYMPTOM, text="5xx", status="verified", corroborating_ids=("E0001",)),
        ),
    )
    assert len(citations(report, ledger)) == 6
    assert citation_precision(report, ledger, SupportCheck()) == pytest.approx(4 / 6)


def test_citation_precision_is_zero_without_citations_and_recall_counts_any_ledger_item():
    ledger = make_ledger()
    report = make_report(Verdict.INCONCLUSIVE, [hyp(1)])
    assert citation_precision(report, ledger, SupportCheck()) == 0.0  # Req 27.10
    assert key_evidence_recall(ledger, label_key()) == 0.5  # log-2 found, chg-1 not; citing is not needed
    assert key_evidence_recall(ledger, insufficient_key()) is None  # Req 27.11


def test_support_check_rejects_historical_and_wrong_component_support():
    ledger = make_ledger()
    check = SupportCheck()
    report = make_report(Verdict.CONCLUSIVE, [hyp(1, "orders-db-456", support=("E0001",)), hyp(2, support=("E0002",))])
    assert citation_precision(report, ledger, check) == 0.0


def test_score_run_is_deterministic_and_records_expected_gaps():
    ledger = make_ledger()
    gap = DataGap(gap_id="G001", source=ToolName.QUERY_LOGS, reason="timeout", recorded_at=at(0))
    report = make_report(Verdict.CONCLUSIVE, [hyp(1, support=("E0001",))], data_gaps=(gap,))
    key = label_key(
        expected_data_gaps=(
            ExpectedDataGap(source="query_logs", reason="timeout"),
            ExpectedDataGap(source="query_metrics", reason="timeout"),
        )
    )
    trace = RunTrace(run_id="r", system=SystemName.MULTI)
    first = score_run(RUN, report, ledger, trace, key, latency_s=1.5)
    second = score_run(RUN, report, ledger, trace, key, latency_s=1.5)
    assert first == second  # Req 27.9
    assert (first.top1, first.top3, first.hit_at_3, first.over_confident) == (1, 1, 1, 0)
    assert first.citation_precision == 1.0 and first.key_evidence_recall == 0.5
    assert first.expected_gaps_found == {"query_logs:timeout": True, "query_metrics:timeout": False}


def test_run_seed_depends_only_on_seed_scenario_and_index():
    assert run_seed(42, "a", 1) == run_seed(42, "a", 1)
    assert len({run_seed(42, "a", 1), run_seed(42, "a", 2), run_seed(42, "b", 1), run_seed(43, "a", 1)}) == 4


# Aggregation and the Results_Table (Req 26.5, 26.9, 27.8, 27.17)


def score(scenario="s1", system=SystemName.MULTI, index=1, *, failed=False, top1=0, hit=0, recall=0.5, precision=0.5, tools=3, confident=0):
    return RunScore(
        scenario=scenario,
        system=system,
        run_index=index,
        seed=index,
        failed=failed,
        error="boom" if failed else None,
        top1=top1,
        top3=top1,
        hit_at_3=hit,
        over_cautious=None if failed else 0,
        over_confident=None if failed else confident,
        citation_precision=0.0 if failed else precision,
        key_evidence_recall=(0.0 if recall is not None else None) if failed else recall,
        tool_calls=tools,
        latency_s=1.0,
        input_tokens=10,
        output_tokens=5,
        total_tokens=15,
    )


def test_failed_runs_count_in_accuracy_but_not_in_other_metrics():
    cell = [score(index=1, top1=1, tools=2, precision=1.0), score(index=2, failed=True, tools=99), score(index=3, top1=1, tools=4, precision=0.5)]
    top1 = metric_stat(cell, MetricKey.TOP1_ACCURACY)
    assert top1.n == 3 and top1.mean == round(2 / 3, 4) and top1.spread == round(statistics.stdev([1, 0, 1]), 4)
    tools = metric_stat(cell, MetricKey.TOOL_CALLS)
    assert (tools.n, tools.mean, tools.spread) == (2, 3.0, round(statistics.stdev([2, 4]), 4))
    assert metric_stat(cell, MetricKey.CITATION_PRECISION).mean == 0.75
    assert metric_stat(cell, MetricKey.OVER_CONFIDENT_RATE).n == 2  # Req 27.17: over non-failed Runs
    single = metric_stat(cell[:1], MetricKey.TOOL_CALLS)
    assert single.spread == 0.0 and single.n == 1


def test_all_failed_cell_and_not_applicable_metrics():
    all_failed = [score(index=i, failed=True) for i in (1, 2)]
    assert metric_stat(all_failed, MetricKey.TOP1_ACCURACY).status == "ok"
    assert metric_stat(all_failed, MetricKey.RANKING_HIT_AT_3).mean == 0.0
    for metric in (MetricKey.CITATION_PRECISION, MetricKey.KEY_EVIDENCE_RECALL, MetricKey.TOOL_CALLS, MetricKey.LATENCY_S, MetricKey.OVER_CAUTIOUS_RATE):
        assert metric_stat(all_failed, metric).status == "unavailable", metric
    insufficient = [score(index=1, hit=None, recall=None), score(index=2, hit=None, recall=None, failed=True)]
    assert metric_stat(insufficient, MetricKey.RANKING_HIT_AT_3).status == "not_applicable"
    assert metric_stat(insufficient, MetricKey.KEY_EVIDENCE_RECALL).status == "not_applicable"


def test_aggregate_rows_and_pooled_all_row():
    scores = [
        score("s2", SystemName.BASELINE, 1, top1=1),
        score("s1", SystemName.MULTI, 1, top1=1),
        score("s1", SystemName.BASELINE, 1),
        score("s2", SystemName.MULTI, 1, failed=True),
    ]
    rows = aggregate(scores)
    assert [(r.scenario, r.system) for r in rows] == [
        ("s1", "multi"),
        ("s1", "baseline"),
        ("s2", "multi"),
        ("s2", "baseline"),
        (ALL_ROW, "multi"),
        (ALL_ROW, "baseline"),
    ]
    all_multi = rows[4]
    assert all_multi.runs == 2 and all_multi.failed_runs == 1
    assert all_multi.metrics[MetricKey.TOP1_ACCURACY].mean == 0.5
    assert all_multi.metrics[MetricKey.TOOL_CALLS].n == 1
    assert rows[2].metrics[MetricKey.CITATION_PRECISION].status == "unavailable"


def test_results_markdown_matches_json():
    scores = [score(index=1, top1=1), score(index=2), score(index=3, failed=True), score("s2", SystemName.BASELINE, 1, hit=None, recall=None)]
    table = ResultsTable(
        provider="openai",
        model="gpt-5.4-mini",
        seed=42,
        runs_per_scenario=3,
        evaluation_date=date(2026, 10, 21),
        scenario_count=2,
        eval_command="incident-investigator eval --config config.toml --out results/",
        systems=(SystemName.MULTI, SystemName.BASELINE),
        rows=tuple(aggregate(scores)),
        runs=tuple(scores),
    )
    restored = ResultsTable.model_validate(json.loads(table.model_dump_json()))
    markdown = render_results_markdown(table)
    for text in ("openai", "gpt-5.4-mini", "Seed: 42", "Runs per scenario: 3", "2026-10-21", table.eval_command):
        assert text in markdown
    lines = markdown.splitlines()
    for row in restored.rows:
        row_lines = [l for l in lines if l.startswith(f"| {row.scenario} | {row.system} | {row.runs} | {row.failed_runs} |")]
        assert len(row_lines) == 2  # one in the main table, one in the diagnostics table
        joined = " ".join(row_lines)
        for metric, stat in row.metrics.items():
            assert f"| {format_stat(stat)} " in joined, (row.scenario, metric)
    assert "| s1 | multi | 3 | boom |" in markdown  # failed runs listed with their error
    assert "not measure real LLM accuracy" not in markdown  # only fake-provider tables carry the smoke note
