"""Task_Graph, plan validation and fallback, and Scheduler dispatch (Req 6.1-6.9, 7.1-7.3, 7.5, 16.1, 16.2, 16.5)."""

import itertools
import json
from typing import Any

import pytest
from factories import make_incident_report
from pydantic import BaseModel

from incident_investigator.agents.base import AgentOutcome
from incident_investigator.clock import FakeClock
from incident_investigator.llm.fake import ScriptedFakeLLM, ScriptEntry
from incident_investigator.llm.gateway import LLMGateway
from incident_investigator.models.budget import BudgetLimits
from incident_investigator.models.config import OrchestratorSettings
from incident_investigator.models.outputs import PlanOutput
from incident_investigator.models.tasks import Task
from incident_investigator.models.vocab import AgentName, CallKind, FailureKind, SystemName, TaskKind, TaskState
from incident_investigator.orchestrator.graph import IllegalTransition, TaskFactory, TaskGraph
from incident_investigator.orchestrator.planner import Planner
from incident_investigator.orchestrator.scheduler import Scheduler
from incident_investigator.state.blackboard import Blackboard
from incident_investigator.tracing import TraceRecorder

OBS, DEP, REA, CRI, REP = "observability", "dependency", "reasoning", "critic", "reporting"


FOCUS = itertools.count(1)


def planned(task_id: str, agent: str, *deps: str) -> dict[str, Any]:
    """A planned Task. Specialist Tasks need a distinct, non-blank focus (agents/planner.plan_check)."""
    task = {"task_id": task_id, "agent": agent, "depends_on": list(deps)}
    if agent in (OBS, DEP):
        task["focus"] = f"{task_id} evidence {next(FOCUS)}"
    return task


GOOD_PLAN = [
    planned("logs", OBS),
    planned("metrics", OBS, "logs"),
    planned("changes", DEP),
    planned("reason", REA, "logs", "metrics", "changes"),
    planned("review", CRI, "reason"),
    planned("report", REP),
]


def plan(tasks: list[dict[str, Any]]) -> PlanOutput:
    return PlanOutput.model_validate({"tasks": tasks})


def replace(tasks, task_id, **changes):
    return [{**t, **changes} if t["task_id"] == task_id else t for t in tasks]


# plan validation (Req 6.1, 6.3)


@pytest.mark.parametrize(
    ("tasks", "cause"),
    [
        (replace(GOOD_PLAN, "logs", depends_on=["metrics"]), "cycle"),
        (replace(GOOD_PLAN, "changes", depends_on=["changes"]), "cycle"),  # self-loop
        ([*GOOD_PLAN, planned("logs", OBS)], "duplicate_task_id"),
        (replace(GOOD_PLAN, "review", depends_on=["reason", "ghost"]), "unknown_dependency"),
        (replace(GOOD_PLAN, "reason", depends_on=["logs", "changes"]), "structure"),  # misses a specialist
        (  # no dependency task
            [GOOD_PLAN[0], GOOD_PLAN[1], planned("reason", REA, "logs", "metrics"), GOOD_PLAN[4], GOOD_PLAN[5]],
            "structure",
        ),
        ([*GOOD_PLAN, planned("more", OBS)], "structure"),  # 3 observability tasks > max 2
        (replace(GOOD_PLAN, "report", depends_on=["review"]), "structure"),
        ([*GOOD_PLAN, planned("boss", "orchestrator")], "structure"),
    ],
)
def test_validate_plan_rejects_defects(tasks, cause):
    defect = TaskGraph.validate_plan(plan(tasks), max_initial=2)
    assert defect is not None and defect.cause == cause, defect


def test_valid_plan_gets_run_unique_ids_in_topological_order():
    reordered = [GOOD_PLAN[5], GOOD_PLAN[3], *GOOD_PLAN[:3], GOOD_PLAN[4]]  # reporting and reasoning listed early
    assert TaskGraph.validate_plan(plan(reordered), max_initial=2) is None
    graph = TaskGraph.from_plan(plan(reordered), TaskFactory())
    assert [(t.task_id, t.agent.value) for t in graph] == [
        ("T1", REP), ("T2", OBS), ("T3", OBS), ("T4", DEP), ("T5", REA), ("T6", CRI)
    ]
    assert graph.get("T3").depends_on == ("T2",) and set(graph.get("T5").depends_on) == {"T2", "T3", "T4"}
    assert all(t.state is TaskState.PENDING for t in graph)


def test_default_graph_structure():
    graph = TaskGraph.default(TaskFactory())
    assert [(t.agent.value, t.depends_on) for t in graph] == [
        (OBS, ()), (DEP, ()), (REA, ("T1", "T2")), (CRI, ("T3",)), (REP, ())
    ]


def gateway_with(entries):
    clock = FakeClock()
    trace = TraceRecorder("r", SystemName.MULTI, clock)
    board = Blackboard("r", "inc", make_incident_report(), clock, trace)
    limits = BudgetLimits(critic_rounds=3, tool_calls=10, tokens=100_000, wall_clock_s=60, reporting_reserve=100)
    return LLMGateway(ScriptedFakeLLM(entries, clock), board, trace, limits, "fake"), trace


@pytest.mark.parametrize(
    ("entries", "cause"),
    [
        ([ScriptEntry(AgentName.ORCHESTRATOR, text=json.dumps({"tasks": replace(GOOD_PLAN, "logs", depends_on=["metrics"])}))], "cycle"),
        ([ScriptEntry(AgentName.ORCHESTRATOR, text=json.dumps({"tasks": [*GOOD_PLAN, planned("logs", OBS)]}))], "duplicate_task_id"),
        ([ScriptEntry(AgentName.ORCHESTRATOR, text=json.dumps({"tasks": replace(GOOD_PLAN, "review", depends_on=["x"])}))], "unknown_dependency"),
        ([ScriptEntry(AgentName.ORCHESTRATOR, text="no plan"), ScriptEntry(AgentName.ORCHESTRATOR, text="{}")], "validation_failed"),
        ([], "llm_failed"),
    ],
)
async def test_planner_falls_back_to_the_default_graph_with_a_trace_event(entries, cause):
    gateway, trace = gateway_with(entries)
    graph, fallback = await Planner(gateway, trace, OrchestratorSettings(max_initial_tasks_per_specialist=2)).plan(make_incident_report(), TaskFactory())
    assert fallback == cause
    assert [t.agent.value for t in graph] == [OBS, DEP, REA, CRI, REP]
    (event,) = trace.to_trace().of_type("default_graph_fallback")
    assert event.cause == cause and event.detail


async def test_planner_uses_a_valid_plan():
    gateway, trace = gateway_with([ScriptEntry(AgentName.ORCHESTRATOR, CallKind.PLAN, text=json.dumps({"tasks": GOOD_PLAN}))])
    graph, fallback = await Planner(gateway, trace, OrchestratorSettings(max_initial_tasks_per_specialist=2)).plan(make_incident_report(), TaskFactory())
    assert fallback is None and len(graph) == 6
    assert not trace.to_trace().of_type("default_graph_fallback")


# transitions (Req 6.9)


@pytest.mark.parametrize(
    ("path", "illegal"),
    [
        ([], TaskState.COMPLETED),
        ([], TaskState.FAILED),
        ([TaskState.RUNNING], TaskState.PENDING),
        ([TaskState.RUNNING, TaskState.COMPLETED], TaskState.CANCELLED),
        ([TaskState.CANCELLED], TaskState.RUNNING),
        ([TaskState.RUNNING, TaskState.FAILED], TaskState.RUNNING),
    ],
)
def test_illegal_transitions_raise(path, illegal):
    graph = TaskGraph.default(TaskFactory())
    for state in path:
        graph.transition("T1", state)
    with pytest.raises(IllegalTransition):
        graph.transition("T1", illegal)


def test_ready_waits_for_terminal_dependencies():
    graph = TaskGraph.default(TaskFactory())
    assert [t.task_id for t in graph.ready()] == ["T1", "T2", "T5"]
    graph.transition("T1", TaskState.RUNNING)
    graph.transition("T1", TaskState.FAILED)
    assert "T3" not in [t.task_id for t in graph.ready()]
    graph.transition("T2", TaskState.CANCELLED)
    assert [t.task_id for t in graph.ready()] == ["T3", "T5"]


# dispatch (Req 6.2, 6.5, 6.6, 7.1-7.3, 7.5, 16.1, 16.2, 16.5)


class Done(BaseModel):
    task_id: str


class TimedAgent:
    """Takes a scripted amount of virtual time per Task and fails the given attempts."""

    def __init__(self, clock: FakeClock, durations: dict[str, float], failures: dict[str, set[int]]):
        self.clock = clock
        self.durations = durations
        self.failures = failures
        self.starts: list[str] = []

    async def run(self, task: Task, ctx) -> AgentOutcome:
        self.starts.append(task.task_id)
        await self.clock.sleep(self.durations.get(task.task_id, 1.0))
        if task.attempt in self.failures.get(task.task_id, set()):
            raise RuntimeError(f"scripted failure of {task.task_id}")
        return AgentOutcome(task_id=task.task_id, agent=task.agent, output=Done(task_id=task.task_id))


class Hooks:
    def __init__(self, agent: TimedAgent):
        self.agent = agent
        self.committed: list[str] = []

    def agent_for(self, task):
        return self.agent

    def context_for(self, task):
        return None

    def admit(self, task):
        return True

    def on_rejected(self, task):
        pass

    def on_start(self, task):
        return {}

    async def on_success(self, task, outcome):
        self.committed.append(task.task_id)

    async def after_terminal(self, task):
        pass


def graph_of(*specs: tuple[str, tuple[str, ...]]) -> TaskGraph:
    """Tasks T1.. with the given agents and dependencies (by task ID)."""
    factory = TaskFactory()
    kinds = {OBS: TaskKind.INITIAL, DEP: TaskKind.INITIAL, REA: TaskKind.REASONING, CRI: TaskKind.CRITIC}
    return TaskGraph(factory.new(AgentName(agent), kinds[agent], deps) for agent, deps in specs)


async def dispatch(graph: TaskGraph, *, limit: int, durations=None, failures=None, timeout_s: float = 180.0):
    clock = FakeClock()
    trace = TraceRecorder("r", SystemName.MULTI, clock)
    board = Blackboard("r", "inc", make_incident_report(), clock, trace)
    agent = TimedAgent(clock, durations or {}, failures or {})
    hooks = Hooks(agent)
    scheduler = Scheduler(
        TaskGraph(), hooks=hooks, board=board, trace=trace, clock=clock,
        concurrency_limit=limit, per_task_timeout_s=timeout_s, loop_ended=lambda: False,
    )
    scheduler.extend(list(graph))
    for task in graph:
        await scheduler.wait_terminal(task.task_id)
    return scheduler, trace.to_trace(), board, hooks


def replay(trace, graph: TaskGraph, limit: int) -> list[str]:
    """Check every transition against the limit and the dependencies; return the start order."""
    deps = {t.task_id: t.depends_on for t in graph}
    state: dict[str, str] = {}
    starts = []
    for event in trace.of_type("task_transition"):
        if event.to_state is TaskState.RUNNING:
            assert all(state.get(d) in ("completed", "failed", "cancelled") for d in deps[event.task_id])
            starts.append(event.task_id)
        state[event.task_id] = event.to_state.value
        assert sum(1 for s in state.values() if s == "running") <= limit
    return starts


DIAMOND = ((OBS, ()), (DEP, ()), (OBS, ("T1",)), (DEP, ("T1", "T2")), (REA, ("T1", "T2", "T3", "T4")), (CRI, ("T5",)))


@pytest.mark.parametrize("limit", [1, 2, 3, 16])
async def test_dispatch_is_topological_and_within_the_limit(limit):
    graph = graph_of(*DIAMOND)
    durations = {"T1": 3, "T2": 1, "T3": 2, "T4": 1, "T5": 1, "T6": 1}
    scheduler, trace, _, hooks = await dispatch(graph, limit=limit, durations=durations)
    starts = replay(trace, graph, limit)
    assert sorted(starts) == [f"T{i}" for i in range(1, 7)]
    assert all(scheduler.graph.get(t).state is TaskState.COMPLETED for t in starts)
    assert sorted(hooks.committed) == sorted(starts)


async def test_both_initial_specialists_start_before_either_finishes():
    graph = graph_of((OBS, ()), (DEP, ()), (REA, ("T1", "T2")))
    _, trace, _, _ = await dispatch(graph, limit=2, durations={"T1": 5, "T2": 1})
    seq = [(e.task_id, e.to_state.value) for e in trace.of_type("task_transition")]
    assert seq[:3] == [("T1", "running"), ("T2", "running"), ("T2", "completed")]


async def test_limit_one_runs_specialists_one_at_a_time():
    graph = graph_of((OBS, ()), (DEP, ()), (REA, ("T1", "T2")))
    _, trace, _, _ = await dispatch(graph, limit=1, durations={"T1": 5, "T2": 1})
    seq = [(e.task_id, e.to_state.value) for e in trace.of_type("task_transition")]
    assert seq[:4] == [("T1", "running"), ("T1", "completed"), ("T2", "running"), ("T2", "completed")]


async def test_a_failing_task_leaves_the_others_running():
    graph = graph_of((OBS, ()), (DEP, ()), (REA, ("T1", "T2")))
    scheduler, trace, board, _ = await dispatch(graph, limit=4, durations={"T1": 1, "T2": 10}, failures={"T1": {1, 2}})
    states = {t.task_id: t.state for t in scheduler.graph}
    assert states == {"T1": TaskState.FAILED, "T2": TaskState.COMPLETED, "T3": TaskState.COMPLETED}
    failures = trace.of_type("agent_failure")
    assert [(f.task_id, f.attempt, f.failure_kind) for f in failures] == [
        ("T1", 1, FailureKind.EXCEPTION), ("T1", 2, FailureKind.EXCEPTION)
    ]
    t2 = [e.to_state.value for e in trace.of_type("task_transition") if e.task_id == "T2"]
    assert t2 == ["running", "completed"]  # never cancelled
    (gap,) = board.data_gaps()
    assert (gap.source, gap.task_id, gap.reason) == (AgentName.OBSERVABILITY, "T1", "exception")


async def test_retry_success_records_no_gap_and_timeouts_are_failures():
    graph = graph_of((OBS, ()), (DEP, ()))
    scheduler, trace, board, _ = await dispatch(
        graph, limit=2, durations={"T1": 1, "T2": 500}, failures={"T1": {1}}, timeout_s=100
    )
    assert scheduler.graph.get("T1").state is TaskState.COMPLETED
    assert scheduler.graph.get("T1").attempt == 2
    assert scheduler.graph.get("T2").state is TaskState.FAILED
    kinds = [(f.task_id, f.failure_kind) for f in trace.of_type("agent_failure")]
    assert kinds == [("T1", FailureKind.EXCEPTION), ("T2", FailureKind.TASK_TIMEOUT), ("T2", FailureKind.TASK_TIMEOUT)]
    assert [g.task_id for g in board.data_gaps()] == ["T2"]
    t1 = [e.to_state.value for e in trace.of_type("task_transition") if e.task_id == "T1"]
    assert t1 == ["running", "completed"]  # the retry keeps the Task running
