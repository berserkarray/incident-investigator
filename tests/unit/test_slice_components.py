"""Unit checks for the thin-slice building blocks. The workstreams extend each area."""

import pytest
from factories import at, make_incident_report, make_snapshot, window

from incident_investigator.clock import FakeClock
from incident_investigator.errors import BudgetBlocked, ImmutableEvidenceError
from incident_investigator.llm.base import ValidationContext
from incident_investigator.llm.fake import FakeScriptExhausted, ScriptedFakeLLM, ScriptEntry
from incident_investigator.llm.gateway import LLMGateway
from incident_investigator.llm.validator import StructuredOutputFailure
from incident_investigator.models.budget import BudgetLimits, TokenCount
from incident_investigator.models.evidence import LedgerEntry, LedgerView
from incident_investigator.models.incident import ClaimStatusUpdate, IncidentClaim, IncidentClaimKind
from incident_investigator.models.outputs import CriticOutput, HypothesisDraft, RecommendedActionDraft
from incident_investigator.models.report import ReportHypothesis
from incident_investigator.models.snapshot import LogRecord
from incident_investigator.models.tools import QueryLogsInput
from incident_investigator.models.vocab import (
    AgentName,
    CallKind,
    ClaimStatus,
    ErrorCode,
    LogLevel,
    Reliability,
    SystemName,
    TerminationReason,
    ToolName,
    Verdict,
)
from incident_investigator.state.blackboard import Blackboard
from incident_investigator.state.citations import CitableOutput, CitationValidator
from incident_investigator.state.verdict import investigator_verdict
from incident_investigator.tools.impl import cut_to_limit, query_logs
from incident_investigator.tools.layer import BoardToolBudget, ToolLayer
from incident_investigator.tools.summary import ObservationSummarizer
from incident_investigator.tracing import TraceRecorder

# query_logs (Req 13.3, 13.9, 13.12, 13.13)


def logs_snapshot():
    logs = (
        LogRecord(record_id="b", timestamp=at(10), service="checkout-a1b", level=LogLevel.ERROR, message="Pool TIMEOUT"),
        LogRecord(record_id="a", timestamp=at(10), service="checkout-a1b", level=LogLevel.ERROR, message="pool timeout"),
        LogRecord(record_id="c", timestamp=at(20), service="checkout-a1b", level=LogLevel.INFO, message="ok", attributes={"cohort": "beta"}),
        LogRecord(record_id="d", timestamp=at(30), service="checkout-a1b", level=LogLevel.ERROR, message="boundary"),
        LogRecord(record_id="e", timestamp=at(15), service="payments-c2d", level=LogLevel.ERROR, message="pool timeout"),
    )
    return make_snapshot(logs=logs)


def test_query_logs_filters_and_orders():
    snap = logs_snapshot()
    base = {"service": "checkout-a1b", "time_range": window(0, 30)}
    ids = [r.record_id for r in query_logs(QueryLogsInput(**base), snap)]
    assert ids == ["a", "b", "c"]  # half-open: d at the end is excluded; ties by record ID
    assert [r.record_id for r in query_logs(QueryLogsInput(**base, levels=["error"], text="POOL"), snap)] == ["a", "b"]
    assert [r.record_id for r in query_logs(QueryLogsInput(**base, attributes={"cohort": "beta"}), snap)] == ["c"]
    records, truncated = cut_to_limit(query_logs(QueryLogsInput(**base), snap), 2)
    assert [r.record_id for r in records] == ["a", "b"] and truncated


# ToolLayer (Req 3.2-3.6, 13.8, 17.1, 18.2)


def tool_layer(limit: int = 60):
    clock = FakeClock()
    trace = TraceRecorder("r", SystemName.MULTI, clock)
    board = Blackboard("r", "inc", make_incident_report(), clock, trace)
    signals = []

    class Signal:
        def signal(self, reason):
            signals.append(reason)

    layer = ToolLayer(
        snapshot=logs_snapshot(),
        ledger=board,
        gaps=board,
        budget=BoardToolBudget(board, limit),
        trace=trace,
        clock=clock,
        summarizer=ObservationSummarizer(5),
        termination=Signal(),
    )
    return layer, board, trace, signals


def params(**extra):
    return {"service": "checkout-a1b", "time_range": window(0, 30).model_dump(mode="json"), **extra}


async def test_invalid_input_has_no_side_effects():
    layer, board, trace, _ = tool_layer()
    for bad in (params(limit=0), params(service="nope"), {"service": "checkout-a1b"}):
        obs = await layer.request(AgentName.OBSERVABILITY, "T1", "query_logs", bad)
        assert obs.error.code is ErrorCode.INVALID_INPUT and obs.evidence_id is None
    obs = await layer.request(AgentName.OBSERVABILITY, "T1", "query_traces", params())
    assert obs.error.code is ErrorCode.INVALID_INPUT
    assert board.usage.tool_calls == 0 and not board.evidence() and not board.data_gaps()
    assert not trace.to_trace().of_type("tool_call") and len(trace.to_trace().of_type("tool_rejected")) == 4


async def test_successful_request_appends_one_evidence_item():
    layer, board, trace, _ = tool_layer()
    obs = await layer.request(AgentName.OBSERVABILITY, "T1", "query_logs", params(limit=2))
    (item,) = board.evidence()
    assert obs.evidence_id == item.evidence_id == "E0001"
    assert item.reliability is Reliability.COMPLETE and obs.truncated
    assert item.source_record_ids == ("a", "b")
    assert board.records_for("E0001")[0].record_id == "a"
    # equal counts are ordered by value, so the upper-case variant sorts first
    assert [t.value for t in item.observation.top_values] == [
        "error checkout-a1b: Pool TIMEOUT",
        "error checkout-a1b: pool timeout",
    ]
    assert board.usage.tool_calls == 1 and len(trace.to_trace().of_type("tool_call")) == 1

    empty = await layer.request(AgentName.OBSERVABILITY, "T1", "query_logs", params(text="absent"))
    assert empty.error is None and empty.evidence_id == "E0002"
    assert board.evidence()[1].observation.note == "no records matched"


async def test_budget_exhaustion_records_a_gap_and_signals():
    layer, board, _, signals = tool_layer(limit=1)
    await layer.request(AgentName.OBSERVABILITY, "T1", "query_logs", params())
    obs = await layer.request(AgentName.OBSERVABILITY, "T1", "query_logs", params(levels=["error"]))
    assert obs.error.code is ErrorCode.BUDGET_EXHAUSTED
    assert board.usage.tool_calls == 1
    (gap,) = board.data_gaps()
    assert gap.source is ToolName.QUERY_LOGS and gap.reason == "budget_exhausted"
    assert signals == [TerminationReason.TOOL_CALL_BUDGET_EXHAUSTED]


# Blackboard (Req 1.4, 3.7, 3.9)


async def test_blackboard_claims_revisions_and_immutability():
    board = Blackboard("r", "inc", make_incident_report(symptoms=["a", "b"]), FakeClock())
    claims = await board.record_initial_claims()
    assert [c.claim_id for c in claims] == ["IC1", "IC2", "IC3", "IC4"]
    assert all(c.status is ClaimStatus.UNVERIFIED and c.label == "assumption" for c in claims)
    draft = HypothesisDraft(component="c", cause_type="config_error", statement="s", confidence=0.5, rank=1, evidence_sufficient=False)
    await board.add_hypothesis_revisions("T3", [("H1", draft)])
    await board.add_hypothesis_revisions("T7", [("H1", draft.model_copy(update={"confidence": 0.7}))])
    assert [r.revision for r in board.hypothesis_history("H1")] == [1, 2]
    assert board.active_hypotheses()[0].confidence == 0.7
    with pytest.raises(ImmutableEvidenceError):
        board.update_evidence("E0001", reliability="partial")


# LLM gateway and Fake_LLM (Req 5.3-5.6, 5.12, 18.3, 18.4, 30.4)


def gateway(entries, tokens=1000, reserve=100):
    clock = FakeClock()
    trace = TraceRecorder("r", SystemName.MULTI, clock)
    board = Blackboard("r", "inc", make_incident_report(), clock, trace)
    fake = ScriptedFakeLLM(entries, clock)
    limits = BudgetLimits(critic_rounds=3, tool_calls=10, tokens=tokens, wall_clock_s=60, reporting_reserve=reserve)
    return LLMGateway(fake, board, trace, limits, "fake-model"), fake, board, trace


async def test_one_repair_then_success_and_usage_recorded():
    usage = TokenCount(input_tokens=10, output_tokens=5)
    gw, fake, board, trace = gateway(
        [
            ScriptEntry(AgentName.CRITIC, CallKind.CRITIC, text="not json", usage=usage),
            ScriptEntry(AgentName.CRITIC, CallKind.REPAIR, text='{"accept_hypothesis_id": "H1"}', usage=usage),
        ]
    )
    out = await gw.structured(AgentName.CRITIC, CallKind.CRITIC, "sys", "user", CriticOutput, ValidationContext())
    assert out.accept_hypothesis_id == "H1"
    assert [p.call_kind for p in fake.prompts] == [CallKind.CRITIC, CallKind.REPAIR]
    assert "not a JSON object" in fake.prompts[1].user
    assert board.usage.total_tokens == 30 and len(trace.to_trace().of_type("llm_call")) == 2


async def test_second_invalid_output_is_an_agent_failure():
    gw, _, _, _ = gateway(
        [ScriptEntry(AgentName.CRITIC, text="{}"), ScriptEntry(AgentName.CRITIC, text='{"accept_hypothesis_id": null}')]
    )
    with pytest.raises(StructuredOutputFailure):
        await gw.structured(AgentName.CRITIC, CallKind.CRITIC, "sys", "user", CriticOutput, ValidationContext())


async def test_missing_script_names_the_caller():
    gw, _, _, _ = gateway([])
    with pytest.raises(FakeScriptExhausted, match="reasoning"):
        await gw.structured(AgentName.REASONING, CallKind.REASONING, "s", "u", CriticOutput, ValidationContext())


async def test_token_gate_keeps_the_reporting_reserve():
    big = TokenCount(input_tokens=900, output_tokens=0)
    gw, _, _, _ = gateway(
        [
            ScriptEntry(AgentName.CRITIC, text='{"accept_hypothesis_id": "H1"}', usage=big),
            ScriptEntry(AgentName.REPORTING, text='{"accept_hypothesis_id": "H1"}', usage=big),
        ]
    )
    await gw.structured(AgentName.CRITIC, CallKind.CRITIC, "s", "u", CriticOutput, ValidationContext())
    with pytest.raises(BudgetBlocked):
        await gw.structured(AgentName.CRITIC, CallKind.CRITIC, "s", "u", CriticOutput, ValidationContext())
    await gw.structured(AgentName.REPORTING, CallKind.REPORTING, "s", "u", CriticOutput, ValidationContext())
    with pytest.raises(BudgetBlocked):
        await gw.structured(AgentName.REPORTING, CallKind.REPORTING, "s", "u", CriticOutput, ValidationContext())


# Citation_Validator (Req 1.6, 4.2-4.6, 4.8)


LEDGER = LedgerView(
    items={
        "E1": LedgerEntry(tool=ToolName.QUERY_LOGS, reliability=Reliability.COMPLETE),
        "E2": LedgerEntry(tool=ToolName.SEARCH_INCIDENTS, reliability=Reliability.COMPLETE),
    }
)
PRIOR = {"IC1": IncidentClaim(claim_id="IC1", kind=IncidentClaimKind.SYMPTOM, text="errors")}


def test_citation_validator_removes_labels_and_is_idempotent():
    draft = HypothesisDraft(
        component="c", cause_type="config_error", statement="s", confidence=0.5, rank=1,
        evidence_sufficient=True, supporting_evidence_ids=("E1", "E9"),
    )
    output = CitableOutput(
        hypotheses=(draft,),
        actions=(RecommendedActionDraft(text="t", category="further_investigation", hypothesis_id="H9"),),
        claim_updates=(ClaimStatusUpdate(claim_id="IC1", status="verified", corroborating_ids=("E2",)),),
    )
    validator = CitationValidator()
    first = validator.validate(output, LEDGER, frozenset({"H1"}), PRIOR, AgentName.REASONING)
    assert first.output.hypotheses[0].supporting_evidence_ids == ("E1",)
    assert first.output.actions == ()
    assert first.output.claim_updates[0].status is ClaimStatus.UNVERIFIED  # historical-only support rejected
    assert {v.target_kind for v in first.violations} == {"hypothesis", "recommended_action", "claim_status"}
    second = validator.validate(first.output, LEDGER, frozenset({"H1"}), PRIOR, "baseline")
    assert second.output == first.output and second.violations == ()


# Verdict rule (Req 19.1, 19.2)


def ranked(support, confidence=0.9):
    return [
        ReportHypothesis(
            hypothesis_id="H1", rank=1, revision=1, component="c", cause_type="config_error", statement="s",
            supporting_evidence_ids=support, confidence=confidence, raw_confidence=confidence, evidence_sufficient=True,
        )
    ]


@pytest.mark.parametrize(
    ("termination", "acceptance", "support", "confidence", "expected"),
    [
        (TerminationReason.CRITIC_ACCEPTED, "H1", ("E1",), 0.9, Verdict.CONCLUSIVE),
        (TerminationReason.MAX_CRITIC_ROUNDS, None, ("E1",), 0.9, Verdict.INCONCLUSIVE),
        (TerminationReason.CRITIC_ACCEPTED, "H2", ("E1",), 0.9, Verdict.INCONCLUSIVE),
        (TerminationReason.CRITIC_ACCEPTED, "H1", ("E1",), 0.5, Verdict.INCONCLUSIVE),
        (TerminationReason.CRITIC_ACCEPTED, "H1", ("E2",), 0.9, Verdict.INCONCLUSIVE),
    ],
)
def test_investigator_verdict(termination, acceptance, support, confidence, expected):
    decision = investigator_verdict(termination, acceptance, ranked(support, confidence), LEDGER, 0.6)
    assert decision.verdict is expected
