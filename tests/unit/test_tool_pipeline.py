"""Failure injection, retries, the Circuit_Breaker and the Tool_Call Budget through the ToolLayer pipeline.

Req 13.8, 13.11, 13.12, 14.3-14.7, 15.1-15.11, 17.1, 17.7, 18.2.
"""

import hashlib
from dataclasses import dataclass, field

import pytest
from factories import at, make_incident_report, make_snapshot, window

from incident_investigator.clock import FakeClock
from incident_investigator.models.base import canonical_json
from incident_investigator.models.config import FailureInjectionSettings
from incident_investigator.models.snapshot import FailureProfile, ForcedFailure, LogRecord
from incident_investigator.models.tools import GetDependenciesInput, QueryLogsInput, ToolError, ToolResult
from incident_investigator.models.vocab import (
    AgentName,
    ErrorCode,
    FailureType,
    LogLevel,
    Reliability,
    SystemName,
    TerminationReason,
    ToolName,
)
from incident_investigator.state.blackboard import Blackboard
from incident_investigator.tools.breaker import Admission, CircuitBreaker
from incident_investigator.tools.impl import cut_to_limit, query_logs
from incident_investigator.tools.injector import FailureInjector, InjectedOutcome
from incident_investigator.tools.layer import BoardToolBudget, ToolLayer
from incident_investigator.tools.retry import RetryPolicy
from incident_investigator.tools.summary import ObservationSummarizer
from incident_investigator.tracing import TraceRecorder

SERVICE = "checkout-a1b"
OBS = AgentName.OBSERVABILITY


def logs_snapshot(n=6, forced=()):
    logs = tuple(
        LogRecord(record_id=f"log-{i:02d}", timestamp=at(10 * i), service=SERVICE, level=LogLevel.ERROR, message=f"error {i}")
        for i in range(n)
    )
    profile = FailureProfile(forced=tuple(ForcedFailure(tool=t, failure_type=f) for t, f in forced))
    return make_snapshot(logs=logs, failure_profile=profile)


def settings(random_enabled=False, timeout_p=0.0, partial_p=0.0, stale_p=0.0):
    return FailureInjectionSettings(random_enabled=random_enabled, timeout_p=timeout_p, partial_p=partial_p, stale_p=stale_p)


def params(**extra):
    return {"service": SERVICE, "time_range": window(0, 120).model_dump(mode="json"), **extra}


class RecordingClock(FakeClock):
    def __init__(self):
        super().__init__()
        self.sleeps: list[float] = []

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        await super().sleep(seconds)


class CountingInjector:
    """Wraps an injector and counts how often the pipeline applies it."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = 0

    def apply(self, tool, inp, attempt, matches):
        self.calls += 1
        return self.inner.apply(tool, inp, attempt, matches)


@dataclass
class ScriptedInjector:
    """Returns a timeout for each True in ``timeouts`` (in call order), then normal results."""

    timeouts: list[bool]
    calls: int = 0

    def apply(self, tool, inp, attempt, matches):
        timed_out = self.calls < len(self.timeouts) and self.timeouts[self.calls]
        self.calls += 1
        if timed_out:
            return ToolResult(tool=tool, error=ToolError(code=ErrorCode.TIMEOUT, message="timeout"))
        records, truncated = cut_to_limit(matches, inp.limit)
        return ToolResult(tool=tool, records=records, truncated=truncated)


@dataclass
class Rig:
    layer: ToolLayer
    board: Blackboard
    trace: TraceRecorder
    clock: RecordingClock
    breaker: CircuitBreaker
    injector: object
    signals: list = field(default_factory=list)

    def events(self, kind):
        return self.trace.to_trace().of_type(kind)

    async def logs(self, **extra):
        return await self.layer.request(OBS, "T1", "query_logs", params(**extra))


def rig(snapshot=None, *, injector=None, seed=42, inject=None, max_attempts=3, base=0.5, cap=8.0, threshold=3, cooldown=30.0, budget=60):
    snapshot = snapshot or logs_snapshot()
    clock = RecordingClock()
    trace = TraceRecorder("r", SystemName.MULTI, clock)
    board = Blackboard("r", snapshot.incident_id, make_incident_report(), clock, trace)
    breaker = CircuitBreaker(threshold, cooldown)
    injector = injector or FailureInjector(seed, snapshot, inject or settings())
    signals: list = []

    class Signal:
        def signal(self, reason):
            signals.append(reason)

    layer = ToolLayer(
        snapshot=snapshot,
        ledger=board,
        gaps=board,
        budget=BoardToolBudget(board, budget),
        trace=trace,
        clock=clock,
        summarizer=ObservationSummarizer(5),
        termination=Signal(),
        injector=injector,
        breaker=breaker,
        retry=RetryPolicy(seed, max_attempts, base, cap),
    )
    return Rig(layer, board, trace, clock, breaker, injector, signals)


def u(text: str) -> float:
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big") / 2**64


# validation (Req 13.8)


async def test_invalid_input_skips_injection_budget_gaps_and_breaker():
    snapshot = logs_snapshot(forced=[(ToolName.QUERY_LOGS, FailureType.TIMEOUT)])
    counting = CountingInjector(FailureInjector(42, snapshot, settings()))
    r = rig(snapshot, injector=counting, threshold=1)
    bad_inputs = (params(limit=501), params(service="nope"), {"service": SERVICE}, params(time_range={"start": at(5).isoformat(), "end": at(5).isoformat()}))
    for bad in bad_inputs:
        obs = await r.layer.request(OBS, "T1", "query_logs", bad)
        assert obs.error.code is ErrorCode.INVALID_INPUT and obs.error.invalid_fields
    assert counting.calls == 0 and r.board.usage.tool_calls == 0 and not r.board.data_gaps()
    assert r.breaker.state(ToolName.QUERY_LOGS).consecutive_failures == 0
    assert not r.events("tool_call") and len(r.events("tool_rejected")) == len(bad_inputs)
    assert r.clock.sleeps == []


async def test_zero_matches_without_injection_is_a_clean_empty_result():
    """Req 13.12, 17.7"""
    r = rig()
    obs = await r.logs(text="absent")
    assert obs.error is None and obs.records == () and not (obs.partial or obs.stale or obs.truncated)
    assert obs.evidence_id == "E0001" and not r.board.data_gaps()


async def test_source_record_ids_include_constituents():
    """Design D9, Req 3.12: buckets and dependency records are found by their raw snapshot IDs."""
    r = rig()
    metrics = await r.layer.request(OBS, "T1", "query_metrics", {**params(), "metric_name": "error_rate"})
    (bucket,) = metrics.records
    item = r.board.evidence_item(metrics.evidence_id)
    assert item.source_record_ids == (bucket.record_id, "met-1")
    assert r.board.record_by_source_id("met-1") == bucket
    deps = await r.layer.request(AgentName.DEPENDENCY, "T2", "get_dependencies", {"service": "lb-0aa", "depth": 2})
    item = r.board.evidence_item(deps.evidence_id)
    assert item.source_record_ids == ("dep-1", "dep-2", "dep-3")  # dep-1 is also the first hop of the others
    assert {d.component for d in deps.records} == {SERVICE, "payments-c2d", "orders-db-456"}


# injected outcome shapes (Req 13.11, 14.3-14.5)


def injector_and_input(n=6, limit=100):
    snapshot = logs_snapshot(n)
    inp = QueryLogsInput(service=SERVICE, time_range=window(0, 120), limit=limit)
    return FailureInjector(7, snapshot, settings()), inp, query_logs(inp, snapshot)


def outcome(injector, inp, failure, attempt=1):
    key = injector.key(ToolName.QUERY_LOGS, inp, attempt)
    return InjectedOutcome(ToolName.QUERY_LOGS, attempt, key, failure)


def test_timeout_shape():
    injector, inp, matches = injector_and_input()
    result = injector.build_result(outcome(injector, inp, FailureType.TIMEOUT), inp, matches)
    assert result.error.code is ErrorCode.TIMEOUT and result.records == ()
    assert not (result.partial or result.stale or result.truncated) and result.as_of is None


@pytest.mark.parametrize("n", [0, 1, 2, 3, 6])
def test_partial_shape(n):
    injector, inp, matches = injector_and_input(n)
    for attempt in range(1, 6):
        result = injector.build_result(outcome(injector, inp, FailureType.PARTIAL, attempt), inp, matches)
        assert result.error is None and result.partial and not result.stale
        ids = [r.record_id for r in result.records]
        full = [r.record_id for r in matches]
        assert ids == [i for i in full if i in ids], "order preserved"
        assert (1 <= len(ids) < n) if n >= 2 else ids == []


def test_partial_applies_to_the_limited_result():
    injector, inp, matches = injector_and_input(6, limit=3)
    result = injector.build_result(outcome(injector, inp, FailureType.PARTIAL), inp, matches)
    assert set(r.record_id for r in result.records) < {"log-00", "log-01", "log-02"}


def test_stale_shape():
    injector, inp, matches = injector_and_input()
    for attempt in range(1, 6):
        result = injector.build_result(outcome(injector, inp, FailureType.STALE, attempt), inp, matches)
        assert result.error is None and result.stale and not result.partial
        assert inp.time_range.start <= result.as_of < inp.time_range.end
        assert result.records == tuple(r for r in matches if r.timestamp <= result.as_of)


async def test_partial_and_stale_results_are_evidence_without_gaps_or_retries():
    """Req 15.3, 17.7"""
    for failure, reliability in ((FailureType.PARTIAL, Reliability.PARTIAL), (FailureType.STALE, Reliability.STALE)):
        r = rig(logs_snapshot(forced=[(ToolName.QUERY_LOGS, failure)]))
        obs = await r.logs()
        (item,) = r.board.evidence()
        assert obs.evidence_id == item.evidence_id and item.reliability is reliability
        assert len(r.events("tool_call")) == 1 and not r.board.data_gaps() and r.clock.sleeps == []
        if failure is FailureType.STALE:
            assert item.time_range.end == obs.as_of


# outcome selection (Req 14.1, 14.6, 14.7)


def test_forced_failure_applies_to_every_attempt_and_seed():
    snapshot = logs_snapshot(forced=[(ToolName.QUERY_LOGS, FailureType.TIMEOUT)])
    inp = QueryLogsInput(service=SERVICE, time_range=window(0, 120))
    for seed in (0, 1, 42, 2**40):
        injector = FailureInjector(seed, snapshot, settings(random_enabled=True, partial_p=1.0))
        assert all(injector.outcome(ToolName.QUERY_LOGS, inp, a).failure is FailureType.TIMEOUT for a in range(1, 11))
        assert injector.outcome(ToolName.QUERY_METRICS, inp, 1).failure is FailureType.PARTIAL  # other Tools: random


def test_random_injection_disabled_injects_nothing():
    snapshot = logs_snapshot()
    injector = FailureInjector(42, snapshot, settings(random_enabled=False, timeout_p=1.0))
    inp = QueryLogsInput(service=SERVICE, time_range=window(0, 120))
    assert all(injector.outcome(tool, inp, a).failure is None for tool in ToolName for a in range(1, 6))


def test_stale_never_selected_for_tools_without_a_time_range():
    snapshot = logs_snapshot()
    injector = FailureInjector(42, snapshot, settings(random_enabled=True, stale_p=1.0))
    inp = GetDependenciesInput(service=SERVICE)
    assert all(injector.outcome(t, inp, a).failure is None for t in (ToolName.GET_DEPENDENCIES, ToolName.SEARCH_INCIDENTS) for a in range(1, 20))
    assert injector.outcome(ToolName.QUERY_LOGS, inp, 1).failure is FailureType.STALE


# retries (Req 15.1-15.3, 15.11, 17.1)


async def test_forced_timeout_retries_to_max_attempts_with_formula_delays():
    r = rig(logs_snapshot(forced=[(ToolName.QUERY_LOGS, FailureType.TIMEOUT)]), max_attempts=4, base=0.5, cap=3.0, threshold=10)
    obs = await r.logs()
    assert obs.error.code is ErrorCode.TIMEOUT and obs.evidence_id is None
    assert [e.attempt for e in r.events("tool_call")] == [1, 2, 3, 4]
    inp = QueryLogsInput(service=SERVICE, time_range=window(0, 120))
    expected = [min(3.0, b + b * u(f"42|query_logs|{canonical_json(inp)}|{k}|jitter")) for k, b in ((1, 0.5), (2, 1.0), (3, 2.0))]
    assert r.clock.sleeps == pytest.approx(expected)
    assert r.clock.sleeps[2] <= 3.0 and r.clock.monotonic() == pytest.approx(sum(expected))
    (gap,) = r.board.data_gaps()
    assert (gap.source, gap.reason, gap.query, gap.affected_time_range) == (ToolName.QUERY_LOGS, "timeout", inp, inp.time_range)


async def test_delays_are_identical_across_runs_and_independent_of_the_incident():
    forced = logs_snapshot(forced=[(ToolName.QUERY_LOGS, FailureType.TIMEOUT)])
    first = rig(forced, threshold=10)
    second = rig(forced.model_copy(update={"incident_id": "inc-other"}), threshold=10)
    await first.logs()
    await second.logs()
    assert first.clock.sleeps == second.clock.sleeps and len(first.clock.sleeps) == 2


async def test_only_timeouts_are_retried():
    r = rig(injector=ScriptedInjector([True, False]))
    obs = await r.logs()
    assert obs.error is None and [e.attempt for e in r.events("tool_call")] == [1, 2]
    assert not r.board.data_gaps() and len(r.clock.sleeps) == 1

    partial = rig(logs_snapshot(forced=[(ToolName.QUERY_LOGS, FailureType.PARTIAL)]))
    await partial.logs()
    assert len(partial.events("tool_call")) == 1 and partial.clock.sleeps == []


def test_retry_policy_cap():
    policy = RetryPolicy(1, 5, 5.0, 6.0)
    inp = QueryLogsInput(service=SERVICE, time_range=window(0, 120))
    assert policy.delay(ToolName.QUERY_LOGS, inp, 2) == 6.0
    assert 5.0 <= policy.delay(ToolName.QUERY_LOGS, inp, 1) <= 6.0


# Circuit_Breaker (Req 15.4-15.8, 15.10)


async def test_breaker_opens_at_threshold_rejects_then_trial_reopens_on_timeout():
    r = rig(logs_snapshot(forced=[(ToolName.QUERY_LOGS, FailureType.TIMEOUT)]), max_attempts=1, threshold=2, cooldown=30)
    await r.logs()
    assert not r.breaker.state(ToolName.QUERY_LOGS).is_open
    await r.logs()
    assert r.breaker.state(ToolName.QUERY_LOGS).is_open

    rejected = await r.logs()
    assert rejected.error.code is ErrorCode.CIRCUIT_OPEN and rejected.records == ()
    assert len(r.events("tool_call")) == 2 and r.board.usage.tool_calls == 2
    (event,) = [e for e in r.events("tool_rejected") if e.code is ErrorCode.CIRCUIT_OPEN]
    assert event.tool == ToolName.QUERY_LOGS and event.input["service"] == SERVICE and event.at == r.clock.now()
    assert [g.reason for g in r.board.data_gaps()] == ["timeout", "timeout", "circuit_open"]

    r.clock.advance(30)
    trial = await r.logs()
    assert trial.error.code is ErrorCode.TIMEOUT and r.events("tool_call")[-1].trial
    state = r.breaker.state(ToolName.QUERY_LOGS)
    assert state.is_open and state.opened_at == r.clock.monotonic()
    assert (await r.logs()).error.code is ErrorCode.CIRCUIT_OPEN  # cooldown restarted


async def test_retry_attempts_are_rejected_once_the_breaker_opens():
    r = rig(logs_snapshot(forced=[(ToolName.QUERY_LOGS, FailureType.TIMEOUT)]), max_attempts=3, threshold=2)
    obs = await r.logs()
    assert obs.error.code is ErrorCode.CIRCUIT_OPEN
    assert len(r.events("tool_call")) == 2 and [g.reason for g in r.board.data_gaps()] == ["circuit_open"]


async def test_successful_trial_closes_the_breaker():
    r = rig(injector=ScriptedInjector([True, True]), max_attempts=1, threshold=2, cooldown=10)
    await r.logs()
    await r.logs()
    assert (await r.logs()).error.code is ErrorCode.CIRCUIT_OPEN
    r.clock.advance(10)
    trial = await r.logs()
    assert trial.error is None and r.events("tool_call")[-1].trial
    assert r.breaker.state(ToolName.QUERY_LOGS).is_open is False
    assert (await r.logs()).error is None and not r.events("tool_call")[-1].trial


async def test_breaker_state_is_per_tool():
    r = rig(logs_snapshot(forced=[(ToolName.QUERY_LOGS, FailureType.TIMEOUT)]), max_attempts=1, threshold=1)
    await r.logs()
    assert r.breaker.state(ToolName.QUERY_LOGS).is_open
    metrics = await r.layer.request(OBS, "T1", "query_metrics", {**params(), "metric_name": "error_rate"})
    assert metrics.error is None and not r.breaker.state(ToolName.QUERY_METRICS).is_open


def test_breaker_state_machine_details():
    breaker = CircuitBreaker(2, 5.0)
    tool = ToolName.GET_DEPLOYMENTS
    assert breaker.admit(tool, 0) is Admission.NORMAL
    breaker.record(tool, Admission.NORMAL, True, 1)
    breaker.record(tool, Admission.NORMAL, False, 2)  # success resets the count
    assert breaker.state(tool).consecutive_failures == 0
    breaker.record(tool, Admission.NORMAL, True, 3)
    breaker.record(tool, Admission.NORMAL, True, 4)
    assert breaker.state(tool).is_open and breaker.admit(tool, 8.9) is Admission.REJECT
    assert breaker.admit(tool, 9) is Admission.TRIAL
    assert breaker.admit(tool, 9) is Admission.REJECT  # only one trial in flight
    breaker.release_trial(tool)
    assert breaker.admit(tool, 9) is Admission.TRIAL
    breaker.record(tool, Admission.NORMAL, False, 9.5)  # a late non-trial success does not close it
    state = breaker.state(tool)
    assert state.is_open and state.consecutive_failures == 0
    breaker.record(tool, Admission.TRIAL, False, 10)
    assert not breaker.state(tool).is_open


# Tool_Call Budget (Req 15.9, 18.2)


async def test_retries_and_trials_count_rejections_do_not():
    r = rig(logs_snapshot(forced=[(ToolName.QUERY_LOGS, FailureType.TIMEOUT)]), max_attempts=3, threshold=3, cooldown=5)
    await r.logs()
    assert r.board.usage.tool_calls == 3
    await r.logs()  # circuit_open: no Tool_Call
    await r.layer.request(OBS, "T1", "query_logs", params(limit=0))  # invalid_input: no Tool_Call
    assert r.board.usage.tool_calls == 3
    r.clock.advance(5)
    await r.logs()  # trial (counts), then its retry is rejected
    assert r.board.usage.tool_calls == 4 == len(r.events("tool_call"))


async def test_budget_exhaustion_mid_retry_records_one_gap_and_signals():
    r = rig(logs_snapshot(forced=[(ToolName.QUERY_LOGS, FailureType.TIMEOUT)]), max_attempts=3, threshold=10, budget=2)
    obs = await r.logs()
    assert obs.error.code is ErrorCode.BUDGET_EXHAUSTED and obs.records == ()
    assert r.board.usage.tool_calls == 2 and len(r.events("tool_call")) == 2
    assert [g.reason for g in r.board.data_gaps()] == ["budget_exhausted"]
    assert r.signals == [TerminationReason.TOOL_CALL_BUDGET_EXHAUSTED]
    assert [e.code for e in r.events("tool_rejected")] == [ErrorCode.BUDGET_EXHAUSTED]


async def test_trial_refused_by_the_budget_is_released():
    r = rig(logs_snapshot(forced=[(ToolName.QUERY_LOGS, FailureType.TIMEOUT)]), max_attempts=1, threshold=1, cooldown=1, budget=1)
    await r.logs()
    r.clock.advance(1)
    assert (await r.logs()).error.code is ErrorCode.BUDGET_EXHAUSTED
    assert r.breaker.state(ToolName.QUERY_LOGS).trial_in_flight is False
