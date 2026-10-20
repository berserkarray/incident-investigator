"""One scripted Run per Termination_Reason, for both systems (tasks 9.2, 9.3; Req 30.6).

Every test asserts exactly one report and that the report and the trace carry the reason.
Tests that need a workstream not merged into this branch yet are skipped with the branch name.
"""

import json
from datetime import timedelta

import pytest
from factories import make_incident_report, make_snapshot
from harness import (
    accept_rank1,
    assert_single_report,
    invalid,
    reasoning_reply,
    request_evidence,
    run_scripted,
    write_incident_dir,
)

from incident_investigator.llm.fake import ScriptEntry
from incident_investigator.models.budget import TokenCount
from incident_investigator.models.critic import EvidenceRequestStatus
from incident_investigator.models.vocab import AgentName, TaskState, TerminationReason, Verdict

NEEDS_ORCHESTRATOR = pytest.mark.skip(reason="needs ws/orchestrator-state")
NEEDS_BASELINE = pytest.mark.skip(reason="needs ws/eval-baseline")


@pytest.fixture
def incident(tmp_path):
    return write_incident_dir(tmp_path / "data", make_snapshot(), make_incident_report())


def reasoning(n: int = 1, **kwargs) -> list[ScriptEntry]:
    return [ScriptEntry(AgentName.REASONING, respond=reasoning_reply(**kwargs)) for _ in range(n)]


def task_of(outcome, agent: AgentName):
    return [t for t in outcome.ledger.tasks if t.agent is agent]


# Investigator (task 9.2)


async def test_critic_accepted(incident, tmp_path):
    script = [*reasoning(), ScriptEntry(AgentName.CRITIC, respond=accept_rank1)]
    outcome = await run_scripted("multi", incident, tmp_path / "out", script)

    assert_single_report(outcome, TerminationReason.CRITIC_ACCEPTED)
    report = outcome.report
    assert report.verdict is Verdict.CONCLUSIVE
    (decision,) = outcome.events("critic_decision")
    assert decision.critic_round == 1 and decision.accepted_hypothesis_id == report.hypotheses[0].hypothesis_id
    assert report.budget_limit is None
    assert report.missing_evidence == ()  # listed only when inconclusive (Req 19.5)


async def test_max_critic_rounds(incident, tmp_path):
    script = [
        *reasoning(2),
        ScriptEntry(AgentName.CRITIC, respond=request_evidence("observability", text="timeout")),
        ScriptEntry(AgentName.CRITIC, respond=request_evidence("observability", text="504")),
    ]
    outcome = await run_scripted("multi", incident, tmp_path / "out", script, budgets={"critic_rounds": 2})

    assert_single_report(outcome, TerminationReason.MAX_CRITIC_ROUNDS)
    report = outcome.report
    assert report.verdict is Verdict.INCONCLUSIVE
    assert [d.critic_round for d in outcome.events("critic_decision")] == [1, 2]
    # Req 19.4: the report names the limit with its configured value and the usage at loop end
    assert report.budget_limit is not None
    assert (report.budget_limit.limit, report.budget_limit.configured, report.budget_limit.used_at_loop_end) == (
        "critic_rounds",
        2,
        2,
    )
    # Req 17.3, 19.5: the last round's request was never tasked, so it is unfulfilled, a Data_Gap and missing evidence
    statuses = {r.request_id: r.status for r in outcome.ledger.evidence_requests}
    assert statuses == {"ER1.1": EvidenceRequestStatus.COMPLETED, "ER2.1": EvidenceRequestStatus.UNFULFILLED}
    assert any(g.reason.startswith("unfulfilled_evidence_request:ER2.1") for g in report.data_gaps)
    assert any(m.source == "evidence_request" and m.ref == "ER2.1" for m in report.missing_evidence)
    assert outcome.ledger.budget.critic_rounds <= 2


async def test_critic_failed(incident, tmp_path):
    script = [*reasoning(), *invalid(AgentName.CRITIC, 4)]  # two attempts, each with one repair
    outcome = await run_scripted("multi", incident, tmp_path / "out", script)

    assert_single_report(outcome, TerminationReason.CRITIC_FAILED)
    assert outcome.report.verdict is Verdict.INCONCLUSIVE
    failures = [e for e in outcome.events("agent_failure") if e.agent is AgentName.CRITIC]
    assert [f.attempt for f in failures] == [1, 2]
    assert len(outcome.prompts_by(AgentName.CRITIC)) == 4
    (critic,) = task_of(outcome, AgentName.CRITIC)
    assert critic.state is TaskState.FAILED
    assert any(str(g.source) == "critic" and g.task_id == critic.task_id for g in outcome.report.data_gaps)


async def test_no_hypotheses(incident, tmp_path):
    outcome = await run_scripted("multi", incident, tmp_path / "out", invalid(AgentName.REASONING, 4))

    assert_single_report(outcome, TerminationReason.NO_HYPOTHESES)
    assert outcome.report.verdict is Verdict.INCONCLUSIVE
    assert outcome.report.hypotheses == ()
    assert not outcome.prompts_by(AgentName.CRITIC)
    assert [t.state for t in task_of(outcome, AgentName.CRITIC)] == [TaskState.CANCELLED]
    assert outcome.ledger.budget.critic_rounds == 0


@NEEDS_ORCHESTRATOR
async def test_duplicate_requests_only(incident, tmp_path):
    same = request_evidence("observability", text="timeout")
    script = [*reasoning(3), ScriptEntry(AgentName.CRITIC, respond=same), ScriptEntry(AgentName.CRITIC, respond=same)]
    outcome = await run_scripted("multi", incident, tmp_path / "out", script, budgets={"critic_rounds": 3})

    assert_single_report(outcome, TerminationReason.DUPLICATE_REQUESTS_ONLY)
    assert outcome.report.verdict is Verdict.INCONCLUSIVE
    first = next(r for r in outcome.ledger.evidence_requests if r.critic_round == 1)
    (discarded,) = outcome.events("duplicate_discarded")
    assert discarded.request.critic_round == 2
    assert discarded.matched_task_id == first.task_id  # Req 29.4: names the matching completed Task
    assert len(outcome.events("critic_decision")) == 2


@NEEDS_ORCHESTRATOR
async def test_tool_call_budget_exhausted(incident, tmp_path):
    outcome = await run_scripted("multi", incident, tmp_path / "out", budgets={"tool_calls": 1})

    assert_single_report(outcome, TerminationReason.TOOL_CALL_BUDGET_EXHAUSTED)
    report = outcome.report
    assert report.verdict is Verdict.INCONCLUSIVE
    assert outcome.ledger.budget.tool_calls == 1 == len(outcome.events("tool_call"))
    assert report.budget_limit is not None and report.budget_limit.limit == "tool_calls"
    assert report.budget_limit.configured == 1
    assert any(g.reason == "budget_exhausted" for g in report.data_gaps)


@NEEDS_ORCHESTRATOR
async def test_token_budget_exhausted(incident, tmp_path):
    tokens, reserve = 200_000, 20_000
    heavy = TokenCount(input_tokens=tokens - 10_000)
    script = [ScriptEntry(AgentName.REASONING, respond=reasoning_reply(), usage=heavy)]
    outcome = await run_scripted(
        "multi",
        incident,
        tmp_path / "out",
        script,
        budgets={"tokens": tokens, "reporting_reserve_tokens": reserve},
    )

    assert_single_report(outcome, TerminationReason.TOKEN_BUDGET_EXHAUSTED)
    report = outcome.report
    assert report.verdict is Verdict.INCONCLUSIVE
    assert report.budget_limit is not None and report.budget_limit.limit == "tokens"
    # Req 18.3: once usage reaches tokens - reserve, only the Reporting_Agent may start a call
    calls = outcome.events("llm_call")
    used, crossed = 0, None
    for index, call in enumerate(calls):
        used += call.input_tokens + call.output_tokens
        if used >= tokens - reserve:
            crossed = index
            break
    assert crossed is not None
    assert all(c.caller is AgentName.REPORTING for c in calls[crossed + 1 :])
    assert not outcome.prompts_by(AgentName.CRITIC)
    # Req 12.7: the reserve is no longer available, so the Deterministic_Report_Generator writes the report
    assert report.built_by == "deterministic"


@NEEDS_ORCHESTRATOR
async def test_wall_clock_timeout(incident, tmp_path):
    timeout = 120
    script = [ScriptEntry(AgentName.REASONING, respond=reasoning_reply(), delay_s=10 * timeout)]
    outcome = await run_scripted(
        "multi", incident, tmp_path / "out", script, budgets={"wall_clock_timeout_s": timeout}
    )

    assert_single_report(outcome, TerminationReason.WALL_CLOCK_TIMEOUT)
    report = outcome.report
    assert report.built_by == "deterministic" and report.verdict is Verdict.INCONCLUSIVE
    assert report.budget_limit is not None and report.budget_limit.limit == "wall_clock_s"
    (started,) = outcome.events("run_started")
    deadline = started.at + timedelta(seconds=timeout)
    # Req 18.5: no LLM call or Tool_Call starts after the timeout; every Task ends cancelled or terminal
    assert all(at < timeout for at, _ in outcome.llm.timeline)
    assert all(e.at < deadline for e in outcome.events("tool_call"))
    assert all(t.state in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED) for t in outcome.ledger.tasks)
    assert [t.state for t in task_of(outcome, AgentName.REPORTING)] == [TaskState.CANCELLED]
    # Req 18.6: the deterministic report is written within 5 s of the timeout
    (ended,) = outcome.events("run_ended")
    assert ended.at <= deadline + timedelta(seconds=5)


# Baseline_Agent (task 9.3)


def baseline_tool_step(incident) -> str:
    start = incident.report.start_time
    params = {
        "service": incident.report.affected_service,
        "time_range": {"start": (start - timedelta(hours=1)).isoformat(), "end": (start + timedelta(hours=1)).isoformat()},
        "levels": ["error"],
        "limit": 20,
    }
    return json.dumps({"reasoning_note": "Check the affected service's errors.", "tool_request": {"tool": "query_logs", "params": params}})


def baseline_final_step(incident, *, outcome: str = "conclusive", support: tuple[str, ...] = ("E0001",)) -> str:
    affected = incident.report.affected_service
    claims = [f"IC{i}" for i in range(1, len(incident.report.symptoms) + 3)]
    answer = {
        "outcome": outcome,
        "hypotheses": [
            {
                "hypothesis_id": "H1",
                "component": affected,
                "cause_type": "config_error",
                "statement": f"config error on {affected}",
                "supporting_evidence_ids": list(support),
                "confidence": 0.9,
                "rank": 1,
                "evidence_sufficient": True,
            },
            {
                "hypothesis_id": "H2",
                "component": "payments-c2d",
                "cause_type": "dependency_timeout",
                "statement": "dependency timeout on payments-c2d",
                "confidence": 0.2,
                "rank": 2,
                "evidence_sufficient": False,
            },
        ],
        "claim_statuses": [{"claim_id": c, "status": "unverified"} for c in claims],
    }
    return json.dumps({"reasoning_note": "The errors start right after the pool change.", "final_answer": answer})


@NEEDS_BASELINE
async def test_baseline_final_answer(incident, tmp_path):
    script = [ScriptEntry("baseline", text=baseline_tool_step(incident)), ScriptEntry("baseline", text=baseline_final_step(incident))]
    outcome = await run_scripted("baseline", incident, tmp_path / "out", script, fallback=False)

    assert_single_report(outcome, TerminationReason.FINAL_ANSWER)
    report = outcome.report
    assert report.system == "baseline" and report.built_by == "baseline"
    assert report.verdict is Verdict.CONCLUSIVE
    assert [h.hypothesis_id for h in report.hypotheses] == ["H1", "H2"]
    assert outcome.ledger.budget.critic_rounds == 0  # Req 25.2: zero Critic_Rounds
    assert len(outcome.events("tool_call")) == 1
    assert {str(p.caller) for p in outcome.prompts} == {"baseline"}  # no other Agent is invoked (Req 25.1)


@NEEDS_BASELINE
async def test_baseline_failed(incident, tmp_path):
    outcome = await run_scripted("baseline", incident, tmp_path / "out", invalid("baseline", 4), fallback=False)

    assert_single_report(outcome, TerminationReason.BASELINE_FAILED)
    report = outcome.report
    assert report.verdict is Verdict.INCONCLUSIVE and report.built_by == "deterministic"
    assert any(str(g.source) == "baseline" and g.step == 1 for g in report.data_gaps)  # Req 25.9
    assert all(c.status == "unverified" for c in report.claim_statuses)


@NEEDS_BASELINE
async def test_baseline_tool_call_budget_stop(incident, tmp_path):
    script = [ScriptEntry("baseline", text=baseline_tool_step(incident)) for _ in range(3)]
    outcome = await run_scripted("baseline", incident, tmp_path / "out", script, fallback=False, budgets={"tool_calls": 1})

    assert_single_report(outcome, TerminationReason.TOOL_CALL_BUDGET_EXHAUSTED)
    report = outcome.report
    assert report.verdict is Verdict.INCONCLUSIVE and report.built_by == "deterministic"
    assert outcome.ledger.budget.tool_calls == 1
