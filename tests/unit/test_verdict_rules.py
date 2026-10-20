"""Verdict rules applied directly to constructed Blackboard states (task 9.4; Req 16.4, 19.1, 19.2, 25.5, 30.7).

Each case leaves exactly one conclusive condition unmet. End-to-end Runs cannot reach most of these
states because the Critic and final-answer validators reject them first (Req 11.5, 11.16).
"""

from dataclasses import dataclass

import pytest
from factories import make_incident_report, window

from incident_investigator.clock import FakeClock
from incident_investigator.models.evidence import ObservationSummary
from incident_investigator.models.gaps import DataGapDraft
from incident_investigator.models.outputs import HypothesisDraft
from incident_investigator.models.tools import QueryLogsInput, SearchIncidentsInput
from incident_investigator.models.vocab import AgentName, Reliability, TerminationReason, ToolName, Verdict
from incident_investigator.report.assembler import ranked_hypotheses
from incident_investigator.state.blackboard import Blackboard
from incident_investigator.state.confidence import apply_confidence_policy
from incident_investigator.state.verdict import baseline_verdict, investigator_verdict

THRESHOLD = 0.5
PENALTY = 0.25

# The ledger every state starts from: one Evidence_Item of each kind the supporting-evidence condition cares about.
COMPLETE, PARTIAL, STALE, HISTORICAL, ABSENT = "E0001", "E0002", "E0003", "E0004", "E0099"


@dataclass(frozen=True)
class State:
    support: tuple[str, ...] = (COMPLETE,)
    raw_confidence: float = 0.9
    gaps: int = 0
    hypotheses: bool = True


async def build_board(state: State) -> Blackboard:
    board = Blackboard("run", "inc", make_incident_report(), FakeClock())
    logs = QueryLogsInput(service="checkout-a1b", time_range=window(0, 120), limit=25)
    history = SearchIncidentsInput(query="pool exhausted", limit=25)
    for tool, query, reliability in (
        (ToolName.QUERY_LOGS, logs, Reliability.COMPLETE),
        (ToolName.QUERY_LOGS, logs, Reliability.PARTIAL),
        (ToolName.QUERY_LOGS, logs, Reliability.STALE),
        (ToolName.SEARCH_INCIDENTS, history, Reliability.COMPLETE),
    ):
        await board.append_evidence(
            tool=tool,
            query=query,
            summary=ObservationSummary(record_count=1),
            time_range=getattr(query, "time_range", None),
            reliability=reliability,
            records=(),
            produced_by=AgentName.OBSERVABILITY,
            task_id="T1",
        )
    assert [e.evidence_id for e in board.evidence()] == [COMPLETE, PARTIAL, STALE, HISTORICAL]
    if state.hypotheses:
        drafts = [
            HypothesisDraft(
                hypothesis_id="H1",
                component="checkout-a1b",
                cause_type="config_error",
                statement="pool size change",
                supporting_evidence_ids=state.support,
                confidence=state.raw_confidence,
                rank=1,
                evidence_sufficient=True,
            ),
            HypothesisDraft(
                hypothesis_id="H2",
                component="payments-c2d",
                cause_type="dependency_timeout",
                statement="slow dependency",
                confidence=0.1,
                rank=2,
                evidence_sufficient=False,
            ),
        ]
        await board.add_hypothesis_revisions("T3", [(d.hypothesis_id, d) for d in drafts])
    for i in range(state.gaps):
        await board.add_gap(DataGapDraft(source=ToolName.QUERY_METRICS, task_id=f"T{10 + i}", reason="timeout"))
    adjustments = apply_confidence_policy(
        board.active_hypotheses(), board.superseded_hypotheses(), board.data_gaps(), PENALTY
    )
    await board.set_adjustments(adjustments)
    return board


async def investigator(state: State, termination=TerminationReason.CRITIC_ACCEPTED, acceptance="H1"):
    board = await build_board(state)
    return investigator_verdict(termination, acceptance, ranked_hypotheses(board), board.ledger_view(), THRESHOLD)


async def baseline(state: State, declared=Verdict.CONCLUSIVE):
    board = await build_board(state)
    return baseline_verdict(declared, ranked_hypotheses(board), board.ledger_view(), THRESHOLD)


def unmet(decision) -> int:
    return sum(not held for held in decision.conditions.values())


# Investigator (Req 19.1, 19.2)


async def test_investigator_conclusive_when_every_condition_holds():
    decision = await investigator(State())
    assert decision.verdict is Verdict.CONCLUSIVE and unmet(decision) == 0


async def test_adjusted_confidence_exactly_at_threshold_is_conclusive():
    # 0.75 - 0.25 * 1 = 0.5 = threshold ("at or above")
    decision = await investigator(State(raw_confidence=0.75, gaps=1))
    assert decision.verdict is Verdict.CONCLUSIVE


@pytest.mark.parametrize(
    ("label", "termination", "acceptance", "state"),
    [
        ("termination is not critic_accepted", TerminationReason.MAX_CRITIC_ROUNDS, "H1", State()),
        ("acceptance does not name rank 1", TerminationReason.CRITIC_ACCEPTED, "H2", State()),
        # raw 0.9 clears the threshold; after two gaps 0.9 - 0.5 = 0.4 does not
        ("adjusted confidence below threshold", TerminationReason.CRITIC_ACCEPTED, "H1", State(gaps=2)),
        ("only support absent from the ledger", TerminationReason.CRITIC_ACCEPTED, "H1", State(support=(ABSENT,))),
        ("only support partial", TerminationReason.CRITIC_ACCEPTED, "H1", State(support=(PARTIAL,))),
        ("only support stale", TerminationReason.CRITIC_ACCEPTED, "H1", State(support=(STALE,))),
        ("only support historical", TerminationReason.CRITIC_ACCEPTED, "H1", State(support=(HISTORICAL,))),
    ],
)
async def test_investigator_one_unmet_condition_is_inconclusive(label, termination, acceptance, state):
    decision = await investigator(state, termination, acceptance)
    assert decision.verdict is Verdict.INCONCLUSIVE, label
    assert unmet(decision) == 1, decision.conditions


async def test_investigator_weak_support_alongside_complete_support_still_concludes():
    decision = await investigator(State(support=(PARTIAL, STALE, HISTORICAL, ABSENT, COMPLETE)))
    assert decision.verdict is Verdict.CONCLUSIVE


async def test_investigator_without_hypotheses_is_inconclusive():
    decision = await investigator(State(hypotheses=False), acceptance=None)
    assert decision.verdict is Verdict.INCONCLUSIVE


# Baseline_Agent (Req 25.5)


async def test_baseline_conclusive_when_every_condition_holds():
    decision = await baseline(State())
    assert decision.verdict is Verdict.CONCLUSIVE and unmet(decision) == 0


@pytest.mark.parametrize(
    ("label", "declared", "state"),
    [
        ("declared inconclusive", Verdict.INCONCLUSIVE, State()),
        ("adjusted confidence below threshold", Verdict.CONCLUSIVE, State(raw_confidence=0.7, gaps=1)),
        ("only support absent from the ledger", Verdict.CONCLUSIVE, State(support=(ABSENT,))),
        ("only support partial", Verdict.CONCLUSIVE, State(support=(PARTIAL,))),
        ("only support stale", Verdict.CONCLUSIVE, State(support=(STALE,))),
        ("only support historical", Verdict.CONCLUSIVE, State(support=(HISTORICAL,))),
    ],
)
async def test_baseline_one_unmet_condition_is_inconclusive(label, declared, state):
    decision = await baseline(state, declared)
    assert decision.verdict is Verdict.INCONCLUSIVE, label
    assert unmet(decision) == 1, decision.conditions


async def test_baseline_final_answer_with_zero_hypotheses_is_inconclusive():
    decision = await baseline(State(hypotheses=False))
    assert decision.verdict is Verdict.INCONCLUSIVE
