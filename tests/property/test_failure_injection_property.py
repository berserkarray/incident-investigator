"""Property 33: Failure injection is deterministic (Req 14.2)."""

import asyncio
from datetime import timedelta

from factories import SERVICES, T0, at, make_incident_report, make_snapshot
from hypothesis import given
from hypothesis import strategies as st

from incident_investigator.clock import FakeClock
from incident_investigator.models.config import FailureInjectionSettings
from incident_investigator.models.snapshot import (
    ChangeEvent,
    FailureProfile,
    ForcedFailure,
    LogRecord,
    MetricPoint,
    PastIncident,
)
from incident_investigator.models.tools import TOOL_INPUT_MODELS
from incident_investigator.models.vocab import (
    BASELINE,
    NO_STALE_TOOLS,
    AgentName,
    ChangeType,
    FailureType,
    LogLevel,
    MetricName,
    SystemName,
    ToolName,
)
from incident_investigator.state.blackboard import Blackboard
from incident_investigator.tools.impl import TOOL_REGISTRY
from incident_investigator.tools.injector import FailureInjector
from incident_investigator.tools.layer import AlwaysClosedBreaker, BoardToolBudget, NoRetry, ToolLayer
from incident_investigator.tools.summary import ObservationSummarizer
from incident_investigator.tracing import TraceRecorder

COMPONENTS = [name for name, _ in SERVICES]
LOG_SERVICES = COMPONENTS[1:4]
METRICS = [MetricName.ERROR_RATE, MetricName.LATENCY_P99_MS]
WORDS = ["pool", "timeout", "disk", "Latency", "errors"]
minutes = st.integers(0, 119)


@st.composite
def snapshots(draw):
    logs = [
        LogRecord(record_id=f"log-{i}", timestamp=at(m), service=s, level=lvl, message=msg)
        for i, (m, s, lvl, msg) in enumerate(
            draw(
                st.lists(
                    st.tuples(minutes, st.sampled_from(LOG_SERVICES), st.sampled_from(list(LogLevel)), st.sampled_from(WORDS)),
                    max_size=12,
                )
            )
        )
    ]
    metrics = [
        MetricPoint(record_id=f"met-{i}", timestamp=at(m), service=s, metric_name=name, value=v)
        for i, (m, s, name, v) in enumerate(
            draw(
                st.lists(
                    st.tuples(minutes, st.sampled_from(LOG_SERVICES), st.sampled_from(METRICS), st.floats(0, 100)),
                    max_size=12,
                )
            )
        )
    ]
    changes = [
        ChangeEvent(record_id=f"chg-{i}", timestamp=at(m), service=s, change_type=c, summary=f"change {i}")
        for i, (m, s, c) in enumerate(
            draw(st.lists(st.tuples(minutes, st.sampled_from(COMPONENTS), st.sampled_from(list(ChangeType))), max_size=5))
        )
    ]
    incidents = [
        PastIncident(
            record_id=f"pin-{i}",
            occurred_at=T0 - timedelta(days=d),
            title=f"{w} incident",
            summary="summary",
            affected_services=(s,),
            symptoms=(w,),
            cause_type="dns_failure",
            resolution="fixed",
        )
        for i, (d, s, w) in enumerate(
            draw(st.lists(st.tuples(st.integers(1, 300), st.sampled_from(LOG_SERVICES), st.sampled_from(WORDS)), max_size=4))
        )
    ]
    return make_snapshot(
        incident_id=draw(st.sampled_from(["inc-aaaa0001", "inc-bbbb0002"])),
        logs=tuple(logs),
        metrics=tuple(metrics),
        changes=tuple(changes),
        past_incidents=tuple(incidents),
    )


@st.composite
def time_ranges(draw):
    start = draw(st.integers(-10, 119))
    end = draw(st.integers(start + 1, 130))
    return {"start": at(start).isoformat(), "end": at(end).isoformat()}


@st.composite
def tool_requests(draw):
    tool = draw(st.sampled_from(list(ToolName)))
    if tool is ToolName.QUERY_LOGS:
        params = {"service": draw(st.sampled_from(LOG_SERVICES)), "time_range": draw(time_ranges())}
        levels = draw(st.none() | st.lists(st.sampled_from([l.value for l in LogLevel]), min_size=1, max_size=3))
        if levels is not None:
            params["levels"] = levels
    elif tool is ToolName.QUERY_METRICS:
        params = {
            "service": draw(st.sampled_from(LOG_SERVICES)),
            "metric_name": draw(st.sampled_from([m.value for m in METRICS])),
            "time_range": draw(time_ranges()),
            "step_seconds": draw(st.sampled_from([60, 300, 600])),
        }
    elif tool is ToolName.GET_DEPLOYMENTS:
        params = {"time_range": draw(time_ranges())}
        if draw(st.booleans()):
            params["service"] = draw(st.sampled_from(COMPONENTS))
    elif tool is ToolName.GET_DEPENDENCIES:
        params = {"service": draw(st.sampled_from(COMPONENTS)), "depth": draw(st.integers(1, 5))}
    else:
        params = {"query": " ".join(draw(st.lists(st.sampled_from(WORDS), min_size=1, max_size=3)))}
    limit = draw(st.none() | st.integers(1, 20))
    if limit is not None:
        params["limit"] = limit
    return tool, params


@st.composite
def injection_settings(draw):
    timeout_p = draw(st.floats(0, 1))
    partial_p = draw(st.floats(0, 1 - timeout_p))
    stale_p = draw(st.floats(0, max(0.0, 1 - timeout_p - partial_p)))
    return FailureInjectionSettings(
        random_enabled=draw(st.booleans()), timeout_p=timeout_p, partial_p=partial_p, stale_p=stale_p
    )


@st.composite
def failure_profiles(draw):
    entries = {}
    for tool in draw(st.lists(st.sampled_from(list(ToolName)), max_size=2, unique=True)):
        types = [f for f in FailureType if not (f is FailureType.STALE and tool in NO_STALE_TOOLS)]
        entries[tool] = draw(st.sampled_from(types))
    return FailureProfile(forced=tuple(ForcedFailure(tool=t, failure_type=f) for t, f in entries.items()))


def parse(tool, params):
    return TOOL_INPUT_MODELS[tool].model_validate({**params, "tool": tool.value})


def shape(result):
    return (result.error.code if result.error else None, result.records, result.partial, result.stale, result.as_of, result.truncated)


async def through_layer(seed, snapshot, settings, target, others, rnd):
    """Run the target request once per caller, interleaved with other requests in a shuffled order."""
    clock = FakeClock()
    trace = TraceRecorder("r", SystemName.MULTI, clock)
    board = Blackboard("r", snapshot.incident_id, make_incident_report(), clock, trace)
    layer = ToolLayer(
        snapshot=snapshot,
        ledger=board,
        gaps=board,
        budget=BoardToolBudget(board, 10_000),
        trace=trace,
        clock=clock,
        summarizer=ObservationSummarizer(5),
        injector=FailureInjector(seed, snapshot, settings),
        breaker=AlwaysClosedBreaker(),
        retry=NoRetry(),
    )
    calls = [(caller, target, True) for caller in (AgentName.OBSERVABILITY, AgentName.DEPENDENCY, BASELINE)]
    calls += [(AgentName.REASONING, other, False) for other in others]
    rnd.shuffle(calls)
    observations = await asyncio.gather(
        *(layer.request(caller, None, tool.value, params) for caller, (tool, params), _ in calls)
    )
    return [obs for obs, (_, _, is_target) in zip(observations, calls) if is_target]


# Feature: incident-investigator, Property 33: Failure injection is deterministic
@given(
    seed=st.integers(0, 2**63),
    snapshot=snapshots(),
    settings=injection_settings(),
    profile=failure_profiles(),
    request=tool_requests(),
    attempt=st.integers(1, 10),
    others=st.lists(tool_requests(), max_size=6),
    rnd=st.randoms(use_true_random=False),
)
def test_property_33_failure_injection_is_deterministic(seed, snapshot, settings, profile, request, attempt, others, rnd):
    """Req 14.2: same outcome across evaluations, equal-after-validation inputs, callers and interleavings."""
    snapshot = snapshot.model_copy(update={"failure_profile": profile})
    tool, params = request
    inp = parse(tool, params)
    reordered = dict(reversed(list(params.items())))
    if "levels" in reordered:
        reordered["levels"] = list(reversed(reordered["levels"]))
    variants = [
        inp,
        parse(tool, reordered),
        TOOL_INPUT_MODELS[tool].model_validate(inp.model_dump(mode="json")),  # every default explicit
        TOOL_INPUT_MODELS[tool].model_validate(inp.model_dump(mode="json", exclude_defaults=True)),
    ]

    def evaluate(variant):
        injector = FailureInjector(seed, snapshot, settings)
        outcome = injector.outcome(tool, variant, attempt)
        return outcome.failure, injector.build_result(outcome, variant, TOOL_REGISTRY[tool](variant, snapshot))

    reference = evaluate(inp)
    assert all(evaluate(v) == reference for v in variants)

    injector = FailureInjector(seed, snapshot, settings)
    matches = TOOL_REGISTRY[tool](inp, snapshot)
    assert all(injector.apply(tool, inp, attempt, matches) == reference[1] for _ in range(3))
    forced = profile.forced_for(tool)
    if forced is not None:
        assert reference[0] is forced

    expected = shape(FailureInjector(seed, snapshot, settings).apply(tool, inp, 1, matches))
    observed = asyncio.run(through_layer(seed, snapshot, settings, request, others, rnd))
    assert [shape(obs) for obs in observed] == [expected] * 3
