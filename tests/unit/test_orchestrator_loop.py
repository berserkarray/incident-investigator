"""Critic loop, termination and loop end on scripted Runs (Req 7.4, 10.4, 11.7-11.14, 12.6, 12.7, 16.3, 16.7,
17.3, 17.10, 18.2-18.10, 19.4, 19.6)."""

import json

import pytest
from scripted_run import (
    CRI,
    DEP,
    OBS,
    OPENING_REQUESTS,
    REA,
    REP,
    WINDOW,
    accept_reply,
    base_script,
    deployments_request,
    entries,
    logs_request,
    no_findings,
    reasoning_reply,
    requests_reply,
    run_scripted,
    usage,
)

from incident_investigator.llm.fake import ScriptEntry
from incident_investigator.models.critic import EvidenceRequest, EvidenceRequestStatus
from incident_investigator.models.tools import QueryLogsInput
from incident_investigator.models.vocab import (
    AgentName,
    CallKind,
    ConcernCategory,
    TaskState,
    TerminationReason,
    ToolName,
    Verdict,
)
from incident_investigator.orchestrator.critic_loop import CriticLoop
from incident_investigator.orchestrator.termination import PRECEDENCE, TerminationArbiter

# CriticLoop de-duplication (Req 11.10)


def request(request_id: str, target=AgentName.OBSERVABILITY, **params) -> EvidenceRequest:
    return EvidenceRequest(
        request_id=request_id,
        critic_round=1,
        target=target,
        hypothesis_id="H1",
        concern=ConcernCategory.MISSING_EVIDENCE,
        question="q",
        suggested_tool=ToolName.QUERY_LOGS,
        suggested_params=QueryLogsInput.model_validate({"service": "checkout-a1b", "time_range": WINDOW, **params}),
        rationale="r",
        status=EvidenceRequestStatus.TASKED,
    )


def test_dedupe_compares_parsed_parameters():
    completed = [("T6", request("ER1.1", levels=["warn", "error"]))]
    round_requests = [
        request("ER2.1", levels=["error", "warn"], limit=100),  # same as T6: level order, explicit default
        request("ER2.2", levels=["error"]),
        request("ER2.3", levels=["error"], time_range={"start": "2024-01-01T01:00:00+01:00", "end": WINDOW["end"]}),
        request("ER2.4", target=AgentName.DEPENDENCY, levels=["error"]),  # other target: kept
    ]
    kept, duplicates = CriticLoop.dedupe(round_requests, completed)
    assert [r.request_id for r in kept] == ["ER2.2", "ER2.4"]
    assert [(d.request.request_id, d.matched_task_id, d.matched_request_id) for d in duplicates] == [
        ("ER2.1", "T6", None),
        ("ER2.3", None, "ER2.2"),  # the same instant written with another UTC offset
    ]
    assert all(d.request.status is EvidenceRequestStatus.DUPLICATE for d in duplicates)


# TerminationArbiter (Req 18.7)


def arbiter(tokens=0, elapsed=0.0, ended=None):
    return TerminationArbiter(
        tokens_used=lambda: tokens,
        token_threshold=100,
        elapsed=lambda: elapsed,
        wall_clock_s=60,
        on_end=(ended.append if ended is not None else None),
    )


@pytest.mark.parametrize("held", [PRECEDENCE[i:] for i in range(len(PRECEDENCE))] + [PRECEDENCE[::-1]])
def test_arbiter_records_the_earliest_listed_condition_once(held):
    ended: list = []
    a = arbiter(ended=ended)
    for reason in held:
        a.signal(reason, check=False)
    assert a.check() == min(held, key=PRECEDENCE.index)
    a.signal(TerminationReason.CRITIC_ACCEPTED)  # later checks change nothing
    assert a.reason == min(held, key=PRECEDENCE.index) and len(ended) == 1


def test_arbiter_reads_token_and_wall_clock_state_at_every_check():
    assert arbiter(tokens=99).check() is None
    assert arbiter(tokens=100).check() is TerminationReason.TOKEN_BUDGET_EXHAUSTED
    assert arbiter(tokens=100, elapsed=60).check() is TerminationReason.TOKEN_BUDGET_EXHAUSTED
    assert arbiter(elapsed=60).check() is TerminationReason.WALL_CLOCK_TIMEOUT
    a = arbiter(tokens=100)
    a.signal(TerminationReason.MAX_CRITIC_ROUNDS, check=False)
    assert a.check() is TerminationReason.MAX_CRITIC_ROUNDS


# scripted Runs


def assert_reason(run, reason: TerminationReason):
    assert run.report.termination_reason is reason
    (event,) = run.events("termination")
    assert event.reason is reason
    assert run.events("run_ended")[0].termination_reason is reason


def starts_before_ends(run, a: str, b: str) -> bool:
    order = [(t, s) for t, _, s in run.transitions()]
    first_end = min(order.index((t, s)) for t in (a, b) for s in ("completed", "failed") if (t, s) in order)
    return order.index((a, "running")) < first_end and order.index((b, "running")) < first_end


@pytest.mark.parametrize("limit", [2, 4])
async def test_feedback_round_then_acceptance(tmp_path, limit):
    critic = [
        *entries(CRI, CallKind.CRITIC, requests_reply(logs_request(), deployments_request())),
        *entries(CRI, CallKind.CRITIC, accept_reply),
    ]
    run = await run_scripted(
        tmp_path, base_script(critic=critic, reasoning_rounds=2), orchestrator={"concurrency_limit": limit}
    )
    assert_reason(run, TerminationReason.CRITIC_ACCEPTED)
    assert run.report.verdict is Verdict.CONCLUSIVE and run.report.built_by == "reporting_agent"
    # round 1 created one Task per request, one Reasoning Task on both and one Critic Task on that
    tasks = {t.task_id: t for t in run.result.ledger.tasks}
    assert (tasks["T6"].agent, tasks["T7"].agent) == (AgentName.OBSERVABILITY, AgentName.DEPENDENCY)
    assert set(tasks["T8"].depends_on) == {"T6", "T7"} and tasks["T9"].depends_on == ("T8",)
    assert starts_before_ends(run, "T6", "T7")  # Req 7.4
    assert [r.status for r in run.result.ledger.evidence_requests] == [EvidenceRequestStatus.COMPLETED] * 2
    assert run.report.budget_usage.critic_rounds.used == 2
    # Hypotheses were revised, not replaced (Req 3.9, 10.4)
    assert [h.revision for h in run.report.hypotheses] == [2, 2]
    decisions = run.events("critic_decision")
    assert [(d.critic_round, d.accepted_hypothesis_id) for d in decisions] == [(1, None), (2, "H1")]
    # Reporting started only after the termination event (Req 6.2)
    seqs = [e.seq for e in run.trace.events if e.type == "task_transition" and e.task_id == "T5"]
    assert run.events("termination")[0].seq < seqs[0]


async def test_duplicates_only_ends_the_loop(tmp_path):
    critic = [
        *entries(CRI, CallKind.CRITIC, requests_reply(logs_request(), logs_request(levels=["error"], limit=25))),
        *entries(CRI, CallKind.CRITIC, requests_reply(logs_request(limit=25))),
    ]
    run = await run_scripted(tmp_path, base_script(critic=critic, reasoning_rounds=2))
    assert_reason(run, TerminationReason.DUPLICATE_REQUESTS_ONLY)
    discarded = run.events("duplicate_discarded")
    assert [(d.request.request_id, d.matched_request_id, d.matched_task_id) for d in discarded] == [
        ("ER1.2", "ER1.1", None),
        ("ER2.1", None, "T6"),
    ]
    statuses = {r.request_id: r.status for r in run.result.ledger.evidence_requests}
    assert statuses == {
        "ER1.1": EvidenceRequestStatus.COMPLETED,
        "ER1.2": EvidenceRequestStatus.DUPLICATE,
        "ER2.1": EvidenceRequestStatus.DUPLICATE,
    }
    assert not run.report.data_gaps  # duplicates are not unfulfilled (Req 17.3)


async def test_requests_in_the_last_round_end_with_max_critic_rounds(tmp_path):
    critic = entries(CRI, CallKind.CRITIC, requests_reply(logs_request()))
    run = await run_scripted(tmp_path, base_script(critic=critic), budgets={"critic_rounds": 1})
    assert_reason(run, TerminationReason.MAX_CRITIC_ROUNDS)
    assert len(run.result.ledger.tasks) == 5  # no Tasks for final-round requests (Req 11.13)
    (gap,) = run.report.data_gaps
    assert gap.reason == "unfulfilled_evidence_request:ER1.1:max_critic_rounds"
    assert gap.source is AgentName.OBSERVABILITY and gap.affected_time_range is not None
    assert run.report.budget_limit.limit == "critic_rounds" and run.report.budget_limit.configured == 1
    assert run.report.verdict is Verdict.INCONCLUSIVE
    assert any(m.source == "evidence_request" and m.ref == "ER1.1" for m in run.report.missing_evidence)
    # the Confidence_Policy counted the unfulfilled-request gap before the Verdict (Req 17.4, 19.6)
    assert run.report.hypotheses[0].confidence == pytest.approx(0.9 - 0.05)


async def test_no_hypotheses_cancels_the_critic_without_a_call(tmp_path):
    script = [*entries(OBS, None, no_findings), *entries(DEP, None, no_findings), *entries(REA, None, "{}", n=4)]
    run = await run_scripted(tmp_path, script + entries(REP, CallKind.REPORTING, '{"recommended_actions": []}'))
    assert_reason(run, TerminationReason.NO_HYPOTHESES)
    assert ("T4", "pending", "cancelled") in run.transitions()
    assert CallKind.CRITIC not in run.kinds() and run.report.budget_usage.critic_rounds.used == 0
    assert run.report.verdict is Verdict.INCONCLUSIVE and run.report.hypotheses == ()
    assert [(g.source, g.reason) for g in run.report.data_gaps] == [(AgentName.REASONING, "structured_output")]


async def test_critic_failure_after_retry_ends_with_critic_failed(tmp_path):
    run = await run_scripted(tmp_path, base_script(critic=entries(CRI, None, "{}", n=4)))
    assert_reason(run, TerminationReason.CRITIC_FAILED)
    assert ("T4", "running", "failed") in run.transitions()
    assert [f.attempt for f in run.events("agent_failure")] == [1, 2]
    assert run.report.budget_usage.critic_rounds.used == 1  # the retry stayed in round 1


async def test_tool_call_budget_exhaustion(tmp_path):
    # The budget covers the four code-issued opening requests (logs and metrics; deployments and
    # dependencies), so the Critic's request in round 1 is the first one refused.
    critic = entries(CRI, CallKind.CRITIC, requests_reply(logs_request()))
    run = await run_scripted(tmp_path, base_script(critic=critic), budgets={"tool_calls": OPENING_REQUESTS})
    assert_reason(run, TerminationReason.TOOL_CALL_BUDGET_EXHAUSTED)
    assert run.report.budget_usage.tool_calls.used == OPENING_REQUESTS
    assert len(run.events("tool_call")) == OPENING_REQUESTS
    reasons = [g.reason for g in run.report.data_gaps]
    assert reasons == ["budget_exhausted", "unfulfilled_evidence_request:ER1.1:tool_call_budget_exhausted"]
    assert ("T6", "running", "cancelled") in run.transitions() and ("T7", "pending", "cancelled") in run.transitions()
    # the budget gap was recorded before the loop ended
    gap_seq = [e.seq for e in run.events("data_gap_recorded")][0]
    assert gap_seq < run.events("termination")[0].seq
    assert run.report.budget_limit.limit == "tool_calls"


async def test_token_budget_below_the_reserve_goes_to_the_deterministic_report(tmp_path):
    script = [
        *entries(OBS, None, no_findings, usage=usage(10)),
        *entries(DEP, None, no_findings, usage=usage(0)),
        *entries(REA, CallKind.REASONING, reasoning_reply, usage=usage(9500)),
        *entries(REP, CallKind.REPORTING, '{"recommended_actions": []}'),
    ]
    run = await run_scripted(tmp_path, script, budgets={"tokens": 10_000, "reporting_reserve_tokens": 1000})
    assert_reason(run, TerminationReason.TOKEN_BUDGET_EXHAUSTED)
    assert CallKind.REPORTING not in run.kinds() and CallKind.CRITIC not in run.kinds()  # Req 12.7, 18.3
    assert ("T5", "pending", "cancelled") in run.transitions()
    assert run.report.built_by == "deterministic"
    assert run.report.budget_limit.limit == "tokens" and run.report.budget_limit.used_at_loop_end == 9510


async def test_reporting_runs_when_the_reserve_is_left(tmp_path):
    script = [
        *entries(OBS, None, no_findings, usage=usage(0)),
        *entries(DEP, None, no_findings, usage=usage(0)),
        *entries(REA, CallKind.REASONING, reasoning_reply, usage=usage(9000)),
        *entries(REP, CallKind.REPORTING, '{"recommended_actions": []}', usage=usage(1000)),
    ]
    run = await run_scripted(tmp_path, script, budgets={"tokens": 10_000, "reporting_reserve_tokens": 1000})
    assert_reason(run, TerminationReason.TOKEN_BUDGET_EXHAUSTED)
    assert run.report.built_by == "reporting_agent"
    assert run.report.budget_usage.tokens.used == 10_000


async def test_failed_reporting_falls_back_with_a_post_verdict_gap(tmp_path):
    script = base_script(critic=entries(CRI, CallKind.CRITIC, accept_reply), reporting=False)
    run = await run_scripted(tmp_path, script + entries(REP, None, "not json", n=4))
    assert_reason(run, TerminationReason.CRITIC_ACCEPTED)
    assert run.report.built_by == "deterministic" and run.report.verdict is Verdict.CONCLUSIVE
    assert [f.attempt for f in run.events("agent_failure")] == [1, 2]
    (gap,) = run.report.data_gaps
    assert (gap.source, gap.after_verdict) == (AgentName.REPORTING, True)
    assert run.report.hypotheses[0].confidence == pytest.approx(0.9)  # no penalty after the Verdict (Req 17.10)


async def test_wall_clock_timeout_cancels_everything_and_builds_the_deterministic_report(tmp_path):
    critic = [ScriptEntry(CRI, CallKind.CRITIC, respond=accept_reply, delay_s=2000)]
    run = await run_scripted(
        tmp_path,
        base_script(critic=critic),
        budgets={"wall_clock_timeout_s": 900},
        orchestrator={"per_task_timeout_s": 5000},
    )
    assert_reason(run, TerminationReason.WALL_CLOCK_TIMEOUT)
    assert run.report.built_by == "deterministic"
    assert run.clock.monotonic() == pytest.approx(900)
    termination = run.events("termination")[0].seq
    assert not [e for e in run.trace.events if e.type in ("llm_call", "tool_call") and e.seq > termination]
    assert {t.state for t in run.result.ledger.tasks if t.task_id in ("T4", "T5")} == {TaskState.CANCELLED}
    assert CallKind.REPORTING not in run.kinds()
    assert run.report.budget_limit.limit == "wall_clock_s"


async def test_timeout_during_reporting_keeps_the_loop_reason(tmp_path):
    script = base_script(critic=entries(CRI, CallKind.CRITIC, accept_reply), reporting=False)
    script.append(ScriptEntry(REP, CallKind.REPORTING, text='{"recommended_actions": []}', delay_s=5000))
    run = await run_scripted(tmp_path, script, orchestrator={"per_task_timeout_s": 9000})
    assert_reason(run, TerminationReason.CRITIC_ACCEPTED)
    assert run.report.built_by == "deterministic" and run.report.verdict is Verdict.CONCLUSIVE
    assert ("T5", "running", "cancelled") in run.transitions()
    assert run.clock.monotonic() == pytest.approx(900)


async def test_valid_plan_is_used(tmp_path):
    plan = {
        "tasks": [
            {"task_id": "a", "agent": "observability", "focus": "errors and latency on checkout"},
            {"task_id": "b", "agent": "dependency", "focus": "changes before the onset"},
            {"task_id": "c", "agent": "reasoning", "depends_on": ["a", "b"]},
            {"task_id": "d", "agent": "critic", "depends_on": ["c"]},
            {"task_id": "e", "agent": "reporting"},
        ]
    }
    script = [ScriptEntry(AgentName.ORCHESTRATOR, CallKind.PLAN, text=json.dumps(plan))]
    script += base_script(critic=entries(CRI, CallKind.CRITIC, accept_reply))
    run = await run_scripted(tmp_path, script)
    assert not run.events("default_graph_fallback")
    assert_reason(run, TerminationReason.CRITIC_ACCEPTED)
    assert [t.agent for t in run.result.ledger.tasks] == [
        AgentName.OBSERVABILITY, AgentName.DEPENDENCY, AgentName.REASONING, AgentName.CRITIC, AgentName.REPORTING
    ]
