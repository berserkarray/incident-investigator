"""Model-level rules that need no run-time context."""

import pytest
from factories import at, make_evidence_item, make_incident_report, make_snapshot, window
from pydantic import ValidationError

from incident_investigator.models.answer_key import AnswerKey
from incident_investigator.models.base import TimeRange, canonical_json
from incident_investigator.models.blackboard import BlackboardState
from incident_investigator.models.claims import DependencyClaim, ObservabilityClaim
from incident_investigator.models.gaps import DataGap
from incident_investigator.models.incident import IncidentReport
from incident_investigator.models.outputs import (
    BaselineFinalAnswer,
    BaselineStep,
    CriticOutput,
    HypothesisDraft,
    ObservabilityStep,
    ReasoningOutput,
    RecommendedActionDraft,
)
from incident_investigator.models.report import (
    BudgetUsageReport,
    IncidentReference,
    InvestigationReport,
    ReportHypothesis,
    UsageLine,
)
from incident_investigator.models.snapshot import FailureProfile, ForcedFailure
from incident_investigator.models.tools import (
    TOOL_INPUT_ADAPTER,
    QueryLogsInput,
    QueryMetricsInput,
    ToolError,
    ToolResult,
)
from incident_investigator.models.vocab import (
    BASELINE_REASONS,
    INVESTIGATOR_REASONS,
    CauseType,
    ErrorCode,
    FailureType,
    LogLevel,
    TerminationReason,
    ToolName,
)


def error_fields(exc: ValidationError) -> set[str]:
    return {".".join(str(p) for p in e["loc"]) for e in exc.errors()}


# Incident_Report (Req 1.1, 1.2)


def test_valid_incident_report_parses():
    report = make_incident_report()
    assert report.start_time.utcoffset() is not None
    assert report.severity == "sev2"


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"title": "   "}, "title"),
        ({"title": "x" * 201}, "title"),
        ({"symptoms": []}, "symptoms"),
        ({"symptoms": ["s"] * 11}, "symptoms"),
        ({"symptoms": ["ok", " "]}, "symptoms.1"),
        ({"affected_service": ""}, "affected_service"),
        ({"start_time": "2024-01-01T10:00:00"}, "start_time"),
        ({"start_time": 1704103200}, "start_time"),
        ({"severity": "critical"}, "severity"),
    ],
)
def test_invalid_incident_report_names_the_field(overrides, field):
    with pytest.raises(ValidationError) as info:
        make_incident_report(**overrides)
    assert field in error_fields(info.value)


def test_missing_and_invalid_fields_are_all_named():
    with pytest.raises(ValidationError) as info:
        IncidentReport.model_validate({"title": "t", "severity": "bad"})
    assert {"symptoms", "affected_service", "start_time", "severity"} <= error_fields(info.value)


# Time ranges and snapshots


def test_time_range_is_half_open_and_ordered():
    tr = window(0, 10)
    assert tr.contains(at(0)) and not tr.contains(at(10))
    with pytest.raises(ValidationError):
        TimeRange(start=at(10), end=at(10))


def test_snapshot_rejects_duplicate_component_names():
    snap = make_snapshot()
    with pytest.raises(ValidationError):
        make_snapshot(components=snap.components + snap.components[:1])


def test_failure_profile_rules():
    FailureProfile(forced=(ForcedFailure(tool=ToolName.QUERY_LOGS, failure_type=FailureType.TIMEOUT),))
    with pytest.raises(ValidationError, match="more than one"):
        FailureProfile(
            forced=(
                ForcedFailure(tool=ToolName.QUERY_LOGS, failure_type=FailureType.TIMEOUT),
                ForcedFailure(tool=ToolName.QUERY_LOGS, failure_type=FailureType.PARTIAL),
            )
        )
    with pytest.raises(ValidationError, match="stale_data is not allowed"):
        FailureProfile(forced=(ForcedFailure(tool=ToolName.GET_DEPENDENCIES, failure_type=FailureType.STALE),))
    with pytest.raises(ValidationError):
        FailureProfile.model_validate({"forced": [{"tool": "query_traces", "failure_type": "timeout"}]})


# Tool inputs and results (Req 13.1, 13.2, 13.8, 13.11)


def test_tool_input_union_and_normalisation():
    params = {
        "tool": "query_logs",
        "service": "checkout-a1b",
        "time_range": {"start": at(0).isoformat(), "end": at(60).isoformat()},
        "levels": ["error", "warn", "error"],
    }
    parsed = TOOL_INPUT_ADAPTER.validate_python(params)
    assert isinstance(parsed, QueryLogsInput)
    assert parsed.levels == (LogLevel.ERROR, LogLevel.WARN)
    assert parsed.limit == 100
    reordered = TOOL_INPUT_ADAPTER.validate_python({**params, "levels": ["warn", "error"], "limit": 100})
    assert canonical_json(parsed) == canonical_json(reordered)


@pytest.mark.parametrize(
    ("bad", "field"),
    [
        ({"limit": 0}, "limit"),
        ({"limit": 501}, "limit"),
        ({"text": ""}, "text"),
        ({"levels": []}, "levels"),
        ({"attributes": {str(i): "v" for i in range(11)}}, "attributes"),
        ({"time_range": {"start": at(60).isoformat(), "end": at(0).isoformat()}}, "time_range"),
    ],
)
def test_invalid_tool_inputs_name_the_field(bad, field):
    params = {"service": "checkout-a1b", "time_range": window(0, 60).model_dump(mode="json"), **bad}
    with pytest.raises(ValidationError) as info:
        QueryLogsInput.model_validate(params)
    assert any(f.startswith(field) for f in error_fields(info.value))


def test_metric_step_must_be_whole_seconds_in_range():
    base = {"service": "s", "metric_name": "error_rate", "time_range": window().model_dump(mode="json")}
    assert QueryMetricsInput.model_validate(base).step_seconds == 300
    for bad in (59, 3601, 300.5):
        with pytest.raises(ValidationError):
            QueryMetricsInput.model_validate({**base, "step_seconds": bad})


def test_tool_result_consistency():
    ToolResult(tool=ToolName.QUERY_LOGS, error=ToolError(code=ErrorCode.TIMEOUT, message="t"))
    with pytest.raises(ValidationError):
        ToolResult(tool=ToolName.QUERY_LOGS, error=ToolError(code=ErrorCode.TIMEOUT, message="t"), partial=True)
    with pytest.raises(ValidationError):
        ToolResult(tool=ToolName.QUERY_LOGS, stale=True)
    ToolResult(tool=ToolName.QUERY_LOGS, stale=True, as_of=at(30))


# Claims and outputs


def test_observability_claim_needs_onset_xor_marker():
    base = {"text": "errors rose", "service": "s", "signal": {"kind": "metric", "metric_name": "error_rate"}}
    ObservabilityClaim.model_validate({**base, "onset": at(61).isoformat(), "evidence_ids": ["E0001"]})
    ObservabilityClaim.model_validate({**base, "no_onset": True, "evidence_ids": ["E0001"]})
    with pytest.raises(ValidationError):
        ObservabilityClaim.model_validate({**base, "evidence_ids": ["E0001"]})
    with pytest.raises(ValidationError):
        ObservabilityClaim.model_validate({**base, "no_onset": True, "evidence_ids": []})


def test_dependency_claim_event_time_and_historical_rules():
    base = {"text": "t", "components": ["c"], "evidence_ids": ["E1"]}
    DependencyClaim.model_validate({**base, "finding_type": "dependency_relation"})
    with pytest.raises(ValidationError):
        DependencyClaim.model_validate({**base, "finding_type": "deployment"})
    with pytest.raises(ValidationError):
        DependencyClaim.model_validate({**base, "finding_type": "past_incident", "event_time": at(0).isoformat()})


def hyp(component: str, rank: int, confidence: float, **extra) -> dict:
    return {
        "component": component,
        "cause_type": "config_error",
        "statement": f"{component} misconfigured",
        "confidence": confidence,
        "rank": rank,
        "evidence_sufficient": True,
        **extra,
    }


def test_reasoning_output_ranking_rules():
    ReasoningOutput.model_validate({"hypotheses": [hyp("a", 1, 0.8), hyp("b", 2, 0.5)]})
    with pytest.raises(ValidationError, match="share a Root_Cause_Label"):
        ReasoningOutput.model_validate({"hypotheses": [hyp("a", 1, 0.8), hyp("a", 2, 0.5)]})
    with pytest.raises(ValidationError, match="ranks must be exactly"):
        ReasoningOutput.model_validate({"hypotheses": [hyp("a", 1, 0.8), hyp("b", 3, 0.5)]})
    with pytest.raises(ValidationError, match="must not increase"):
        ReasoningOutput.model_validate({"hypotheses": [hyp("a", 1, 0.4), hyp("b", 2, 0.5)]})
    with pytest.raises(ValidationError):
        ReasoningOutput.model_validate({"hypotheses": [hyp("a", 1, 0.8)]})


def test_hypothesis_cannot_cite_the_same_id_both_ways():
    with pytest.raises(ValidationError, match="both supporting and contradicting"):
        HypothesisDraft.model_validate(
            hyp("a", 1, 0.5, supporting_evidence_ids=["E1"], contradicting_evidence_ids=["E1"])
        )


def test_critic_output_is_accept_xor_requests():
    CriticOutput(accept_hypothesis_id="H1")
    request = {
        "target": "observability",
        "hypothesis_id": "H1",
        "concern": "low_reliability_support",
        "question": "Is the error rate complete?",
        "suggested_tool": "query_metrics",
        "suggested_params": {},
        "rationale": "only partial data",
    }
    CriticOutput.model_validate({"evidence_requests": [request]})
    with pytest.raises(ValidationError):
        CriticOutput()
    with pytest.raises(ValidationError):
        CriticOutput.model_validate({"accept_hypothesis_id": "H1", "evidence_requests": [request]})
    with pytest.raises(ValidationError, match="observability or dependency"):
        CriticOutput.model_validate({"evidence_requests": [{**request, "target": "reasoning"}]})


def test_actions_cite_evidence_unless_further_investigation():
    RecommendedActionDraft(text="look closer", category="further_investigation", hypothesis_id="H1")
    with pytest.raises(ValidationError):
        RecommendedActionDraft(text="roll back", category="mitigation", hypothesis_id="H1")


def test_step_models_are_exactly_one_of():
    ObservabilityStep.model_validate({"tool_request": {"tool": "query_logs", "params": {}}})
    ObservabilityStep.model_validate({"findings": {"claims": []}})
    with pytest.raises(ValidationError):
        ObservabilityStep.model_validate({})
    with pytest.raises(ValidationError):
        BaselineStep.model_validate({})


def test_baseline_final_answer_needs_unique_ids():
    BaselineFinalAnswer.model_validate({"outcome": "inconclusive"})
    with pytest.raises(ValidationError, match="needs a hypothesis_id"):
        BaselineFinalAnswer.model_validate({"outcome": "conclusive", "hypotheses": [hyp("a", 1, 0.9)]})
    with pytest.raises(ValidationError, match="unique"):
        BaselineFinalAnswer.model_validate(
            {
                "outcome": "conclusive",
                "hypotheses": [hyp("a", 1, 0.9, hypothesis_id="H1"), hyp("b", 2, 0.5, hypothesis_id="H1")],
            }
        )


# Vocabularies, gaps, answer keys, state and reports


def test_termination_reason_sets():
    assert len(INVESTIGATOR_REASONS) == 8 and len(BASELINE_REASONS) == 5
    assert TerminationReason.NO_HYPOTHESES.value == "no_hypotheses"
    assert TerminationReason.FINAL_ANSWER not in INVESTIGATOR_REASONS
    assert CauseType.TRAFFIC_ATTACK in set(CauseType) and len(CauseType) == 14


def test_data_gap_dedupe_key_uses_source_and_query():
    query = QueryLogsInput(service="s", time_range=window())
    a = DataGap(gap_id="G1", recorded_at=at(0), source=ToolName.QUERY_LOGS, query=query, reason="timeout")
    b = DataGap(gap_id="G2", recorded_at=at(1), source=ToolName.QUERY_LOGS, query=query, reason="circuit_open")
    assert a.dedupe_key() == b.dedupe_key()


def test_answer_key_label_matches_outcome():
    common = {"incident_id": "inc-1", "scenario_name": "x", "seed": 1, "canary": "C" * 32}
    AnswerKey(outcome="insufficient_evidence", **common)
    with pytest.raises(ValidationError):
        AnswerKey(outcome="root_cause", **common)
    with pytest.raises(ValidationError):
        AnswerKey(outcome="insufficient_evidence", canary="short", **{k: v for k, v in common.items() if k != "canary"})


def test_evidence_item_is_frozen_and_historical_flag():
    item = make_evidence_item()
    with pytest.raises(ValidationError):
        item.reliability = "partial"  # type: ignore[misc]
    assert not item.historical
    assert make_evidence_item(tool=ToolName.SEARCH_INCIDENTS).historical


def _report(**overrides) -> InvestigationReport:
    line = UsageLine(used=1, limit=10)
    data = {
        "system": "multi",
        "built_by": "deterministic",
        "incident": IncidentReference(
            incident_id="inc-1", title="t", affected_service="s", start_time=at(60), severity="sev2"
        ),
        "verdict": "inconclusive",
        "termination_reason": "max_critic_rounds",
        "budget_usage": BudgetUsageReport(
            tool_calls=line, tokens=line, input_tokens=1, output_tokens=0, critic_rounds=line, wall_clock_s=line
        ),
    }
    data.update(overrides)
    return InvestigationReport.model_validate(data)


def _report_hyp(hid: str, rank: int) -> ReportHypothesis:
    return ReportHypothesis(
        hypothesis_id=hid,
        rank=rank,
        revision=1,
        component="c",
        cause_type="config_error",
        statement="s",
        confidence=0.5,
        raw_confidence=0.5,
        evidence_sufficient=False,
    )


def test_report_round_trips_and_requires_rank_order():
    report = _report(hypotheses=(_report_hyp("H1", 1), _report_hyp("H2", 2)))
    assert InvestigationReport.model_validate_json(report.model_dump_json()) == report
    with pytest.raises(ValidationError, match="rank order"):
        _report(hypotheses=(_report_hyp("H2", 2), _report_hyp("H1", 1)))


def test_blackboard_state_round_trips():
    state = BlackboardState(
        run_id="r1",
        incident_id="inc-1",
        incident_report=make_incident_report(),
        evidence=(make_evidence_item(),),
    )
    assert BlackboardState.model_validate_json(state.model_dump_json()) == state
