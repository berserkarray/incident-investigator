"""Critic feedback loop and parallel dispatch end to end (task 9.5; Req 7.4, 10.4, 11.7-11.11)."""

import pytest
from factories import make_incident_report, make_snapshot
from harness import accept_rank1, context_of, reasoning_reply, request_evidence, run_scripted, write_incident_dir

from incident_investigator.llm.fake import ScriptEntry
from incident_investigator.models.critic import EvidenceRequestStatus
from incident_investigator.models.vocab import AgentName, TaskKind, TaskState, TerminationReason, Verdict


@pytest.fixture
def incident(tmp_path):
    return write_incident_dir(tmp_path / "data", make_snapshot(), make_incident_report())


def reasoning(n: int) -> list[ScriptEntry]:
    return [ScriptEntry(AgentName.REASONING, respond=reasoning_reply()) for _ in range(n)]


def tasks_of(outcome, agent: AgentName):
    return [t for t in outcome.ledger.tasks if t.agent is agent]


async def test_round1_requests_run_concurrently_and_round2_accepts_the_revision(incident, tmp_path):
    script = [
        *reasoning(2),
        ScriptEntry(AgentName.CRITIC, respond=request_evidence("observability", "dependency")),
        ScriptEntry(AgentName.CRITIC, respond=accept_rank1),
    ]
    outcome = await run_scripted("multi", incident, tmp_path / "out", script)
    report, ledger = outcome.report, outcome.ledger

    assert report.termination_reason is TerminationReason.CRITIC_ACCEPTED
    assert report.verdict is Verdict.CONCLUSIVE
    decisions = [(d.critic_round, len(d.evidence_requests), d.accepted_hypothesis_id) for d in outcome.events("critic_decision")]
    assert decisions == [(1, 2, None), (2, 0, "H1")]

    # Req 11.7: one Specialist Task per request, each fulfilled
    requests = sorted(ledger.evidence_requests, key=lambda r: r.request_id)
    assert [(r.request_id, r.target, r.status) for r in requests] == [
        ("ER1.1", AgentName.OBSERVABILITY, EvidenceRequestStatus.COMPLETED),
        ("ER1.2", AgentName.DEPENDENCY, EvidenceRequestStatus.COMPLETED),
    ]
    er_tasks = [r.task_id for r in requests]
    assert all(outcome.task(t).kind is TaskKind.EVIDENCE_REQUEST for t in er_tasks)

    # Req 7.4: both Evidence_Request Tasks are running before either reaches a terminal state
    transitions = outcome.events("task_transition")
    started = {e.task_id: i for i, e in enumerate(transitions) if e.to_state is TaskState.RUNNING}
    ended = {e.task_id: i for i, e in enumerate(transitions) if e.to_state in (TaskState.COMPLETED, TaskState.FAILED)}
    assert max(started[t] for t in er_tasks) < min(ended[t] for t in er_tasks)

    # The Specialist's first Tool_Request in an Evidence_Request Task is the suggested one
    first_call = next(e for e in outcome.events("tool_call") if e.task_id == er_tasks[0])
    assert first_call.input == requests[0].suggested_params.model_dump(mode="json")

    # Req 10.4, 11.8, 11.9: a new Reasoning Task waits for both, then a round-2 Critic Task waits for it
    first_reasoning, second_reasoning = tasks_of(outcome, AgentName.REASONING)
    assert set(second_reasoning.depends_on) == set(er_tasks)
    _, second_critic = tasks_of(outcome, AgentName.CRITIC)
    assert second_critic.depends_on == (second_reasoning.task_id,) and second_critic.critic_round == 2
    reasoning_start = started[second_reasoning.task_id]
    assert all(ended[t] < reasoning_start for t in er_tasks)

    # The Hypothesis is revised, not replaced, once the new evidence is on the board
    history = ledger.hypotheses["H1"]
    assert [r.revision for r in history] == [1, 2]
    assert history[0].task_id == first_reasoning.task_id and history[1].task_id == second_reasoning.task_id
    new_evidence = {e.evidence_id for e in ledger.evidence if e.task_id == er_tasks[0]}
    assert new_evidence
    first_prompt, second_prompt = (context_of(p) for p in outcome.prompts_by(AgentName.REASONING))
    assert not new_evidence & {e["evidence_id"] for e in first_prompt["evidence"]}
    assert new_evidence <= {e["evidence_id"] for e in second_prompt["evidence"]}
    assert report.hypotheses[0].hypothesis_id == "H1" and report.hypotheses[0].revision == 2


@pytest.mark.skip(reason="needs ws/orchestrator-state")
async def test_duplicate_request_is_discarded_with_a_trace_event(incident, tmp_path):
    script = [
        *reasoning(3),
        ScriptEntry(AgentName.CRITIC, respond=request_evidence("observability", text="timeout")),
        # round 2 repeats the round-1 request and adds a new one for the Dependency_Agent
        ScriptEntry(AgentName.CRITIC, respond=request_evidence("observability", "dependency", text="timeout")),
        ScriptEntry(AgentName.CRITIC, respond=accept_rank1),
    ]
    outcome = await run_scripted("multi", incident, tmp_path / "out", script, budgets={"critic_rounds": 3})
    ledger = outcome.ledger

    assert outcome.report.termination_reason is TerminationReason.CRITIC_ACCEPTED
    round1 = next(r for r in ledger.evidence_requests if r.critic_round == 1)
    (discarded,) = outcome.events("duplicate_discarded")
    assert discarded.request.critic_round == 2 and discarded.request.target is AgentName.OBSERVABILITY
    assert discarded.matched_task_id == round1.task_id  # Req 11.10, 29.4
    round2 = [r for r in ledger.evidence_requests if r.critic_round == 2]
    assert [r.target for r in round2 if r.status is EvidenceRequestStatus.COMPLETED] == [AgentName.DEPENDENCY]
    assert all(r.status is EvidenceRequestStatus.DUPLICATE for r in round2 if r.target is AgentName.OBSERVABILITY)
    # no second Task for the duplicate: the initial Observability Task plus the round-1 request only
    assert len(tasks_of(outcome, AgentName.OBSERVABILITY)) == 2
    assert [d.critic_round for d in outcome.events("critic_decision")] == [1, 2, 3]
