"""Blackboard: concurrent writes, immutability, superseded Hypotheses, lookups and round trip (Req 3.7-3.12)."""

import asyncio

import pytest
from factories import at, make_incident_report, window
from hypothesis import given
from hypothesis import strategies as st

from incident_investigator.clock import FakeClock
from incident_investigator.errors import ImmutableEvidenceError
from incident_investigator.models.blackboard import BlackboardState
from incident_investigator.models.budget import TokenCount
from incident_investigator.models.evidence import ObservationSummary
from incident_investigator.models.gaps import DataGapDraft
from incident_investigator.models.outputs import HypothesisDraft
from incident_investigator.models.snapshot import LogRecord
from incident_investigator.models.tools import MetricBucket, QueryLogsInput
from incident_investigator.models.vocab import AgentName, LogLevel, MetricName, Reliability, ToolName
from incident_investigator.state.blackboard import Blackboard

CALLERS = [AgentName.OBSERVABILITY, AgentName.DEPENDENCY, AgentName.REASONING, AgentName.CRITIC, "baseline"]


def board() -> Blackboard:
    return Blackboard("run", "inc", make_incident_report(), FakeClock())


def log(record_id: str, minute: float = 61) -> LogRecord:
    return LogRecord(record_id=record_id, timestamp=at(minute), service="checkout-a1b", level=LogLevel.ERROR, message="boom")


async def append(b: Blackboard, records=(), reliability=Reliability.COMPLETE, task_id="T1"):
    query = QueryLogsInput(service="checkout-a1b", time_range=window(0, 120), limit=25)
    return await b.append_evidence(
        tool=ToolName.QUERY_LOGS,
        query=query,
        summary=ObservationSummary(record_count=len(records)),
        time_range=query.time_range,
        reliability=reliability,
        records=tuple(records),
        produced_by=AgentName.OBSERVABILITY,
        task_id=task_id,
    )


def draft(component: str, cause: str = "config_error", rank: int = 1, confidence: float = 0.5) -> HypothesisDraft:
    return HypothesisDraft(
        component=component, cause_type=cause, statement="s", confidence=confidence, rank=rank, evidence_sufficient=False
    )


# Feature: incident-investigator, Property 6: Concurrent writes are never lost
@given(
    n=st.integers(min_value=1, max_value=200),
    increments=st.lists(
        st.tuples(st.sampled_from(CALLERS), st.integers(0, 5000), st.integers(0, 5000)), min_size=0, max_size=60
    ),
    yields=st.lists(st.integers(0, 3), min_size=1, max_size=16),
)
def test_property_06_concurrent_writes_are_never_lost(n, increments, yields):
    """Req 3.8: N concurrent appends add N items with N distinct IDs; usage rises by the exact sum."""

    async def scenario():
        b = board()
        await append(b)  # an existing item, so "new" items are counted from a non-empty ledger
        before = len(b.evidence())

        async def one_append(i: int):
            for _ in range(yields[i % len(yields)]):
                await asyncio.sleep(0)
            return await append(b, records=(log(f"r{i}"),), task_id=f"T{i}")

        async def one_usage(i: int, caller, tokens_in: int, tokens_out: int):
            for _ in range(yields[(i + 1) % len(yields)]):
                await asyncio.sleep(0)
            await b.add_usage(caller, TokenCount(input_tokens=tokens_in, output_tokens=tokens_out))

        results = await asyncio.gather(
            *(one_append(i) for i in range(n)),
            *(one_usage(i, *inc) for i, inc in enumerate(increments)),
        )
        items = results[:n]
        assert len(b.evidence()) == before + n
        assert len({item.evidence_id for item in items}) == n
        assert [e.seq for e in b.evidence()] == list(range(1, before + n + 1))
        expected_in = sum(i for _, i, _ in increments)
        expected_out = sum(o for _, _, o in increments)
        assert b.usage.tokens == TokenCount(input_tokens=expected_in, output_tokens=expected_out)
        assert sum(t.total for t in b.usage.per_caller.values()) == expected_in + expected_out
        for item in items:  # every append kept its own records
            (record,) = b.records_for(item.evidence_id)
            assert b.record_by_source_id(record.record_id) == record

    asyncio.run(scenario())


async def test_evidence_items_cannot_be_modified_or_deleted():
    b = board()
    item = await append(b, records=(log("r1"),))
    with pytest.raises(ImmutableEvidenceError):
        b.update_evidence(item.evidence_id, reliability="partial")
    with pytest.raises(ImmutableEvidenceError):
        b.delete_evidence(item.evidence_id)
    with pytest.raises(Exception):  # frozen model: no in-place assignment
        b.evidence()[0].reliability = Reliability.PARTIAL  # type: ignore[misc]
    assert b.evidence() == (item,)


async def test_superseded_hypotheses_keep_every_revision():
    b = board()
    await b.add_hypothesis_revisions("T3", [("H1", draft("checkout-a1b")), ("H2", draft("payments-c2d", rank=2, confidence=0.4))])
    await b.add_hypothesis_revisions("T7", [("H1", draft("checkout-a1b", confidence=0.8)), ("H3", draft("orders-db-456", rank=2, confidence=0.2))])
    assert [h.hypothesis_id for h in b.active_hypotheses()] == ["H1", "H3"]
    assert [(h.hypothesis_id, h.revision) for h in b.superseded_hypotheses()] == [("H2", 1)]
    assert [r.revision for r in b.hypothesis_history("H1")] == [1, 2]
    assert b.hypothesis_history("H1")[0].confidence == 0.5  # earlier revision unchanged


async def test_lookup_by_source_record_id_includes_constituents():
    b = board()
    bucket = MetricBucket(
        record_id="bkt-1",
        timestamp=at(60),
        service="checkout-a1b",
        metric_name=MetricName.ERROR_RATE,
        step_seconds=600,
        mean=0.1,
        min=0.0,
        max=0.2,
        count=2,
        constituent_ids=("met-1", "met-2"),
    )
    item = await append(b, records=(bucket,))
    assert item.source_record_ids == ("bkt-1", "met-1", "met-2")
    assert b.record_by_source_id("met-2") == bucket
    assert b.record_by_source_id("absent") is None
    assert b.usage.tool_calls == 0  # lookups never count as Tool_Calls


async def test_round_trip_through_json_and_from_state():
    b = board()
    await b.record_initial_claims()
    await append(b, records=(log("r1"), log("r2", 62)))
    await append(b, reliability=Reliability.STALE)
    await b.add_hypothesis_revisions("T3", [("H1", draft("checkout-a1b")), ("H2", draft("payments-c2d", rank=2, confidence=0.4))])
    await b.add_hypothesis_revisions("T7", [("H2", draft("payments-c2d", confidence=0.6)), ("H3", draft("orders-db-456", rank=2, confidence=0.2))])
    await b.add_gap(DataGapDraft(source=ToolName.QUERY_LOGS, reason="timeout"))
    await b.add_usage(AgentName.REASONING, TokenCount(input_tokens=10, output_tokens=3))
    b.reserve_tool_call(5)
    b.increment_critic_round()
    state = b.to_state()

    parsed = BlackboardState.model_validate_json(state.model_dump_json())
    assert parsed == state
    restored = Blackboard.from_state(parsed, FakeClock())
    assert restored.to_state() == state
    assert restored.record_by_source_id("r2") == b.record_by_source_id("r2")
    assert restored.next_hypothesis_id() == "H4"  # counters continue after the restored IDs
    item = await append(restored)
    assert item.evidence_id == "E0003"
