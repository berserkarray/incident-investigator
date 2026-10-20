"""Agents, prompts, the planner rules and the OfflineResponder (task 7; example tests from 7.10).

Tools that this branch's Tool_Layer does not have yet are stood in for by tiny registry entries,
so the agents can be exercised against every Tool they own.
"""

import json
from datetime import timedelta

import pytest
from factories import SERVICES, at, make_config, make_incident_report, make_snapshot, window

from incident_investigator.agents.base import AgentContext
from incident_investigator.agents.dependency import DependencyAgent, check_dependency
from incident_investigator.agents.observability import ObservabilityAgent
from incident_investigator.agents.planner import plan_check, request_plan
from incident_investigator.agents.prompts import templates
from incident_investigator.agents.prompts.builders import (
    BaselineTurn,
    baseline_prompt,
    critic_prompt,
    reasoning_prompt,
)
from incident_investigator.agents.reasoning import assign_hypothesis_ids
from incident_investigator.clock import FakeClock
from incident_investigator.llm.base import LLMRequest, ValidationContext
from incident_investigator.llm.context import extract_context
from incident_investigator.llm.fake import ScriptedFakeLLM, ScriptEntry
from incident_investigator.llm.gateway import LLMGateway
from incident_investigator.llm.offline import OfflineResponder
from incident_investigator.models.base import TimeRange
from incident_investigator.models.budget import BudgetLimits
from incident_investigator.models.claims import DependencyClaim
from incident_investigator.models.critic import EvidenceRequest, EvidenceRequestStatus
from incident_investigator.models.evidence import LedgerEntry, LedgerView
from incident_investigator.models.outputs import (
    BaselineStep,
    DependencyFindings,
    HypothesisDraft,
    ObservabilityFindings,
    PlanOutput,
)
from incident_investigator.models.snapshot import PastIncident
from incident_investigator.models.tasks import Task
from incident_investigator.models.tools import DependencyRecord, MetricBucket, QueryLogsInput
from incident_investigator.models.vocab import (
    SPECIALIST_TOOLS,
    AgentName,
    CallKind,
    ChangeType,
    ConcernCategory,
    Reliability,
    SystemName,
    TaskKind,
    ToolName,
)
from incident_investigator.state.blackboard import Blackboard
from incident_investigator.tools.impl import TOOL_REGISTRY, sort_records
from incident_investigator.tools.layer import BoardToolBudget, ScopedToolPort, ToolLayer
from incident_investigator.tools.summary import ObservationSummarizer
from incident_investigator.tracing import TraceRecorder

INCIDENT = make_incident_report()  # checkout-a1b, start at(60)

# stand-ins for the Tools that task 5.1 adds


def _metrics(inp, snap):
    return sort_records(
        [
            MetricBucket(
                record_id=f"b-{p.record_id}", timestamp=p.timestamp, service=p.service, metric_name=p.metric_name,
                step_seconds=inp.step_seconds, mean=p.value, min=p.value, max=p.value, count=1,
                constituent_ids=(p.record_id,),
            )
            for p in snap.metrics
            if p.service == inp.service and p.metric_name == inp.metric_name and inp.time_range.contains(p.timestamp)
        ]
    )


def _deployments(inp, snap):
    return sort_records(
        [
            c
            for c in snap.changes
            if (inp.service is None or c.service == inp.service)
            and inp.time_range.contains(c.timestamp)
            and (inp.change_types is None or c.change_type in inp.change_types)
        ]
    )


def _dependencies(inp, snap):
    return [
        DependencyRecord(
            record_id=e.record_id, component=e.dependency, component_kind=snap.component_kind(e.dependency),
            direction="upstream", hops=1, constituent_ids=(),
        )
        for e in snap.dependencies
        if e.dependent == inp.service
    ]


PAST = PastIncident(
    record_id="pi-1", occurred_at=at(-10_000), title="Checkout pool exhaustion", summary="pool", affected_services=("checkout-a1b",),
    symptoms=("5xx",), cause_type="connection_pool_exhaustion", resolution="raised pool size",
)


def _incidents(inp, snap):
    return [PAST]


REGISTRY = {
    **TOOL_REGISTRY,
    ToolName.QUERY_METRICS: _metrics,
    ToolName.GET_DEPLOYMENTS: _deployments,
    ToolName.GET_DEPENDENCIES: _dependencies,
    ToolName.SEARCH_INCIDENTS: _incidents,
}


class Env:
    def __init__(self, entries=None, *, offline=False, tool_calls=60, registry=REGISTRY, **orchestrator):
        self.clock = FakeClock()
        self.trace = TraceRecorder("r", SystemName.MULTI, self.clock)
        self.snapshot = make_snapshot()
        self.board = Blackboard("r", self.snapshot.incident_id, INCIDENT, self.clock, self.trace)
        self.config = make_config(orchestrator=orchestrator).for_system_under_test()
        self.layer = ToolLayer(
            snapshot=self.snapshot,
            ledger=self.board,
            gaps=self.board,
            budget=BoardToolBudget(self.board, tool_calls),
            trace=self.trace,
            clock=self.clock,
            summarizer=ObservationSummarizer(5),
            registry=registry,
        )
        self.llm = OfflineResponder() if offline else ScriptedFakeLLM(entries or [], self.clock)
        limits = BudgetLimits(critic_rounds=3, tool_calls=tool_calls, tokens=10**7, wall_clock_s=900, reporting_reserve=100)
        self.gateway = LLMGateway(self.llm, self.board, self.trace, limits, "fake-model")

    def context(self, agent: AgentName, *, first: bool = True) -> AgentContext:
        return AgentContext(
            board=self.board,
            llm=self.gateway,
            tools=ScopedToolPort(self.layer, agent, SPECIALIST_TOOLS[agent]),
            clock=self.clock,
            config=self.config,
            incident=INCIDENT,
            components=self.snapshot.component_names,
            is_first_task=first,
        )

    def requests(self) -> list[tuple[str, dict]]:
        """Every Tool_Request in order, whether it became a Tool_Call or was rejected."""
        events = self.trace.to_trace().events
        return [(e.tool, e.input) for e in events if e.type in ("tool_call", "tool_rejected")]


def task(agent: AgentName, kind: TaskKind = TaskKind.INITIAL, **extra) -> Task:
    return Task(task_id="T1", agent=agent, kind=kind, created_seq=1, **extra)


DONE = json.dumps({"reasoning": "enough", "findings": {"claims": []}})


# code-issued Tool_Requests (Req 8.2, 9.2, 9.6, 8.7, 9.7)


async def test_observability_first_task_opens_with_logs_and_metrics():
    env = Env([ScriptEntry(AgentName.OBSERVABILITY, CallKind.SPECIALIST_STEP, text=DONE)])
    await ObservabilityAgent().run(task(AgentName.OBSERVABILITY), env.context(AgentName.OBSERVABILITY))
    (logs_tool, logs), (metrics_tool, metrics) = env.requests()
    assert logs_tool == "query_logs" and metrics_tool == "query_metrics"
    assert logs["service"] == metrics["service"] == "checkout-a1b"
    assert {k: logs[k] for k in ("levels", "limit")} == {"levels": ["error", "warn"], "limit": 25}
    assert {k: metrics[k] for k in ("metric_name", "step_seconds", "limit")} == {
        "metric_name": "error_rate", "step_seconds": 600, "limit": 25,
    }
    for query in (logs, metrics):  # [start - 1 h margin, start + 2 h)
        assert TimeRange.model_validate(query["time_range"]) == TimeRange(start=at(0), end=at(180))


async def test_dependency_first_task_opens_with_lookback_deployments_and_dependencies():
    env = Env([ScriptEntry(AgentName.DEPENDENCY, CallKind.SPECIALIST_STEP, text=DONE)])
    await DependencyAgent().run(task(AgentName.DEPENDENCY), env.context(AgentName.DEPENDENCY))
    (deploy_tool, deploy), (deps_tool, deps) = env.requests()
    assert (deploy_tool, deps_tool) == ("get_deployments", "get_dependencies")
    assert deploy["service"] == "checkout-a1b" and deploy["limit"] == 25
    assert deploy["change_types"] == sorted(c.value for c in ChangeType)
    item = env.board.evidence()[0]
    assert item.query.time_range.start == at(60) - timedelta(days=7) and item.query.time_range.end == at(60)
    assert deps == {"tool": "get_dependencies", "service": "checkout-a1b", "depth": 5, "limit": 25}


async def test_evidence_request_task_starts_with_the_suggested_request():
    env = Env([ScriptEntry(AgentName.OBSERVABILITY, CallKind.SPECIALIST_STEP, text=DONE)])
    suggested = QueryLogsInput(service="payments-c2d", time_range=window(30, 90), text="pool", limit=10)
    await env.board.upsert_evidence_request(
        EvidenceRequest(
            request_id="ER1.1", critic_round=1, target=AgentName.OBSERVABILITY, hypothesis_id="H1",
            concern=ConcernCategory.MISSING_EVIDENCE, question="pool errors upstream?", suggested_tool=ToolName.QUERY_LOGS,
            suggested_params=suggested, rationale="check upstream", status=EvidenceRequestStatus.TASKED,
        )
    )
    er_task = task(AgentName.OBSERVABILITY, TaskKind.EVIDENCE_REQUEST, evidence_request_id="ER1.1")
    await ObservabilityAgent().run(er_task, env.context(AgentName.OBSERVABILITY, first=False))
    ((tool, params),) = env.requests()
    assert tool == "query_logs" and params == suggested.model_dump(mode="json")
    context = extract_context(env.llm.prompts[0].user)
    assert context["task"]["evidence_request"]["question"] == "pool errors upstream?"


# step limit and error tolerance (Req 8.4-8.6, 9.5, 9.8, 17.8)


def always_request(tool: str, params: dict):
    return lambda request: json.dumps({"reasoning": "more", "tool_request": {"tool": tool, "params": params}})


async def test_step_limit_counts_every_request_and_errors_never_raise():
    # Budget 1: the opening logs request is the only Tool_Call; the rest are rejected with budget_exhausted.
    bad = {"service": "not-a-service", "time_range": window(0, 60).model_dump(mode="json")}
    entries = [ScriptEntry(AgentName.OBSERVABILITY, CallKind.SPECIALIST_STEP, respond=always_request("query_logs", bad))] * 5
    entries.append(ScriptEntry(AgentName.OBSERVABILITY, CallKind.SPECIALIST_FINDINGS, text='{"claims": []}'))
    env = Env(entries, tool_calls=1, per_task_step_limit=4)
    outcome = await ObservabilityAgent().run(task(AgentName.OBSERVABILITY), env.context(AgentName.OBSERVABILITY))
    assert outcome.tool_requests == 4 and len(env.requests()) == 4
    assert [p.call_kind for p in env.llm.prompts] == [CallKind.SPECIALIST_STEP] * 2 + [CallKind.SPECIALIST_FINDINGS]
    codes = [e.code for e in env.trace.to_trace().of_type("tool_rejected")]
    assert [c.value for c in codes] == ["budget_exhausted", "invalid_input", "invalid_input"]
    assert isinstance(outcome.output, ObservabilityFindings)


async def test_step_limit_without_citable_evidence_needs_no_findings_call():
    bad = {"service": "not-a-service", "time_range": window(0, 60).model_dump(mode="json")}
    entries = [ScriptEntry(AgentName.DEPENDENCY, CallKind.SPECIALIST_STEP, respond=always_request("get_dependencies", bad))] * 2
    env = Env(entries, per_task_step_limit=2)
    outcome = await DependencyAgent().run(task(AgentName.DEPENDENCY), env.context(AgentName.DEPENDENCY, first=False))
    assert outcome.tool_requests == 2 and outcome.output == DependencyFindings()
    assert [p.call_kind for p in env.llm.prompts] == [CallKind.SPECIALIST_STEP] * 2


async def test_a_tool_outside_the_agents_own_tools_fails_validation_and_is_repaired():
    wrong = json.dumps({"reasoning": "x", "tool_request": {"tool": "get_deployments", "params": {}}})
    env = Env(
        [
            ScriptEntry(AgentName.OBSERVABILITY, CallKind.SPECIALIST_STEP, text=wrong),
            ScriptEntry(AgentName.OBSERVABILITY, CallKind.REPAIR, text=DONE),
        ]
    )
    await ObservabilityAgent().run(task(AgentName.OBSERVABILITY), env.context(AgentName.OBSERVABILITY, first=False))
    assert [p.call_kind for p in env.llm.prompts] == [CallKind.SPECIALIST_STEP, CallKind.REPAIR]
    assert "tool_request.tool" in env.llm.prompts[1].user
    assert env.requests() == []


# Dependency claim rules (Req 9.3, 9.4)


LEDGER = LedgerView(
    items={
        "E1": LedgerEntry(tool=ToolName.GET_DEPLOYMENTS, reliability=Reliability.COMPLETE),
        "E2": LedgerEntry(tool=ToolName.SEARCH_INCIDENTS, reliability=Reliability.COMPLETE),
    }
)


def dep_claim(finding: str, ids: tuple[str, ...], **extra) -> DependencyClaim:
    return DependencyClaim(
        text="t", components=("checkout-a1b",), finding_type=finding, evidence_ids=ids,
        event_time=None if finding == "dependency_relation" else at(50), **extra,
    )


@pytest.mark.parametrize(
    ("claim", "ok"),
    [
        (dep_claim("past_incident", ("E2",), historical=True), True),
        (dep_claim("config_change", ("E1",)), True),
        (dep_claim("past_incident", ("E1", "E2"), historical=True), False),
        (dep_claim("past_incident", ("E9",), historical=True), False),
        (dep_claim("config_change", ("E1", "E2")), False),
    ],
)
def test_dependency_historical_rule(claim, ok):
    vctx = ValidationContext(ledger=LEDGER, valid_components=frozenset({"checkout-a1b"}))
    assert (check_dependency(DependencyFindings(claims=(claim,)), vctx) == []) is ok


# Reasoning: code-assigned identity and sufficiency (Req 10.8, 10.9)


async def test_hypothesis_ids_follow_labels_and_sufficiency_needs_complete_current_support():
    board = Blackboard("r", "inc", INCIDENT, FakeClock())

    def draft(component, cause, rank, support=(), sufficient=True):
        return HypothesisDraft(
            component=component, cause_type=cause, statement="s", confidence=1 - rank / 10, rank=rank,
            evidence_sufficient=sufficient, supporting_evidence_ids=support,
        )

    await board.add_hypothesis_revisions("T3", [("H1", draft("checkout-a1b", "config_error", 1))])
    board.next_hypothesis_id()  # H1 is taken
    pairs = assign_hypothesis_ids(
        [draft("payments-c2d", "dependency_timeout", 1, ("E2",)), draft("checkout-a1b", "config_error", 2, ("E1",))],
        board,
        LEDGER,
        board.next_hypothesis_id,
    )
    assert [(hid, d.component) for hid, d in pairs] == [("H2", "payments-c2d"), ("H1", "checkout-a1b")]
    assert [d.evidence_sufficient for _, d in pairs] == [False, True]  # E2 is historical; E1 is complete


# prompts (Req 10.7, 11.15, 24.8)


async def test_reasoning_and_critic_prompts_carry_summaries_not_records():
    env = Env([ScriptEntry(AgentName.OBSERVABILITY, CallKind.SPECIALIST_STEP, text=DONE)])
    await ObservabilityAgent().run(task(AgentName.OBSERVABILITY), env.context(AgentName.OBSERVABILITY))
    await env.board.add_hypothesis_revisions(
        "T3",
        [("H1", HypothesisDraft(component="checkout-a1b", cause_type="config_error", statement="s", confidence=0.7,
                                rank=1, evidence_sufficient=True, supporting_evidence_ids=("E0001",)))],
    )
    prompts = [
        reasoning_prompt(env.board, INCIDENT, env.snapshot.component_names),
        critic_prompt(env.board, INCIDENT, env.snapshot.component_names, critic_round=1, round_budget=3, max_requests=4),
    ]
    for prompt in prompts:
        context = extract_context(prompt)
        for item in env.board.evidence():
            (entry,) = [e for e in context["evidence"] if e["evidence_id"] == item.evidence_id]
            assert entry["tool"] == item.tool.value and entry["reliability"] == item.reliability.value
            assert entry["query"] == item.query.model_dump(mode="json")
            assert entry["observation"] == item.observation.model_dump(mode="json", exclude_defaults=True)
        for item in env.board.evidence():
            for record in env.board.records_for(item.evidence_id):
                assert record.model_dump_json() not in prompt and record.record_id not in prompt
    critic = extract_context(prompts[1])
    assert critic["top_hypothesis_support"]["complete_current"] == ["E0001"]


def test_templates_name_no_component_and_ask_for_small_limits():
    names = {name for name, _ in SERVICES}
    for template in templates.ALL_TEMPLATES.values():
        assert not any(name in template for name in names)
    for owner in ("observability", "dependency", "baseline"):
        assert "limit to 25 or less" in templates.ALL_TEMPLATES[owner]


# planner (Req 6.1, 6.4)


async def test_offline_plan_passes_the_plan_rules():
    env = Env(offline=True)
    plan = await request_plan(env.gateway, INCIDENT, sorted(env.snapshot.component_names), env.config.orchestrator)
    assert [t.agent for t in plan.tasks] == [
        AgentName.OBSERVABILITY, AgentName.DEPENDENCY, AgentName.REASONING, AgentName.CRITIC, AgentName.REPORTING,
    ]
    assert env.llm.prompts[0].caller is AgentName.ORCHESTRATOR and env.llm.prompts[0].call_kind is CallKind.PLAN


def test_plan_rules_reject_blank_focus_misplaced_and_distant_time_ranges():
    config = make_config().for_system_under_test()
    plan = PlanOutput.model_validate(
        {
            "tasks": [
                {"task_id": "o1", "agent": "observability", "focus": " "},
                {"task_id": "d1", "agent": "dependency", "focus": "changes",
                 "time_range": {"start": at(60 * 24 * 30).isoformat(), "end": at(60 * 24 * 31).isoformat()}},
                {"task_id": "r1", "agent": "reasoning", "depends_on": ["o1", "d1"],
                 "time_range": window(0, 60).model_dump(mode="json")},
                {"task_id": "c1", "agent": "critic", "depends_on": ["r1"]},
                {"task_id": "p1", "agent": "reporting"},
            ]
        }
    )
    problems = plan_check(INCIDENT, config.orchestrator)(plan, ValidationContext())
    assert [p.split(":")[0] for p in problems] == ["tasks.0.focus", "tasks.1.time_range", "tasks.2.time_range"]


# OfflineResponder (design D4)


async def test_offline_dependency_findings_validate_and_cite_the_records():
    env = Env(offline=True)
    outcome = await DependencyAgent().run(task(AgentName.DEPENDENCY), env.context(AgentName.DEPENDENCY))
    claims = {c.finding_type.value: c for c in outcome.output.claims}
    assert claims["config_change"].event_time == at(50) and claims["config_change"].components == ("checkout-a1b",)
    assert set(claims["dependency_relation"].components) == {"checkout-a1b", "payments-c2d", "orders-db-456"}


async def test_offline_baseline_probes_then_answers_with_a_valid_final_answer():
    env = Env(offline=True)
    port = ScopedToolPort(env.layer, "baseline", None)
    claims = await env.board.record_initial_claims()
    history: list[BaselineTurn] = []
    for step in range(1, 8):
        user = baseline_prompt(
            incident=INCIDENT, components=env.snapshot.component_names, claims=claims, history=history, step=step
        )
        request = LLMRequest(
            caller="baseline", call_kind=CallKind.BASELINE_STEP, model="m", system=templates.BASELINE, user=user
        )
        out = BaselineStep.model_validate_json((await env.llm.complete(request)).text)
        if out.final_answer is not None:
            break
        assert out.tool_request.params["limit"] <= 25
        obs = await port.request(None, out.tool_request.tool, out.tool_request.params)
        history.append(BaselineTurn(step, out.reasoning_note, out.tool_request.tool, out.tool_request.params, obs))
    assert [t.tool for t in history] == [
        "query_logs", "query_metrics", "get_deployments", "get_dependencies", "search_incidents",
    ]
    assert all(t.observation.error is None for t in history)
    answer = out.final_answer
    assert answer.hypotheses and {s.claim_id for s in answer.claim_statuses} == {c.claim_id for c in claims}
    assert all(s.corroborating_ids for s in answer.claim_statuses if s.status.value == "verified")
    assert all(a.hypothesis_id in {h.hypothesis_id for h in answer.hypotheses} for a in answer.recommended_actions)
