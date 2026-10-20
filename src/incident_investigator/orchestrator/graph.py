"""The Task_Graph: Tasks, Ready_Tasks, allowed transitions, plan validation and the Default_Task_Graph (Req 6).

A Task's dependencies must already be in the graph when it is added, so the graph stays acyclic by
construction and insertion order is a topological order.
"""

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Literal

from ..models.base import TimeRange
from ..models.outputs import PlanOutput, PlannedTask
from ..models.tasks import Task, TaskBrief, TaskGraphState
from ..models.vocab import TERMINAL_TASK_STATES, AgentName, TaskKind, TaskState

ALLOWED_TRANSITIONS: frozenset[tuple[TaskState, TaskState]] = frozenset(
    {
        (TaskState.PENDING, TaskState.RUNNING),
        (TaskState.PENDING, TaskState.CANCELLED),
        (TaskState.RUNNING, TaskState.COMPLETED),
        (TaskState.RUNNING, TaskState.FAILED),
        (TaskState.RUNNING, TaskState.CANCELLED),
    }
)
"""The only Task state changes (Req 6.9)."""

SPECIALISTS = (AgentName.OBSERVABILITY, AgentName.DEPENDENCY)

_KIND_FOR_AGENT = {
    AgentName.OBSERVABILITY: TaskKind.INITIAL,
    AgentName.DEPENDENCY: TaskKind.INITIAL,
    AgentName.REASONING: TaskKind.REASONING,
    AgentName.CRITIC: TaskKind.CRITIC,
    AgentName.REPORTING: TaskKind.REPORTING,
}


class IllegalTransition(Exception):
    def __init__(self, task_id: str, from_state: TaskState, to_state: TaskState):
        super().__init__(f"task {task_id}: {from_state} -> {to_state} is not an allowed transition")
        self.task_id = task_id
        self.from_state = from_state
        self.to_state = to_state


PlanDefectCause = Literal["cycle", "duplicate_task_id", "unknown_dependency", "structure"]


@dataclass(frozen=True)
class PlanDefect:
    """Why a planned Task_Graph was discarded (Req 6.3); ``cause`` names the trace fallback cause."""

    cause: PlanDefectCause
    detail: str


class TaskFactory:
    """Creates Tasks with run-unique IDs ``T1``, ``T2``, ... in creation order."""

    def __init__(self) -> None:
        self._seq = 0

    def new(
        self,
        agent: AgentName,
        kind: TaskKind,
        depends_on: tuple[str, ...] = (),
        *,
        critic_round: int | None = None,
        evidence_request_id: str | None = None,
        focus: str = "",
        time_range: TimeRange | None = None,
    ) -> Task:
        self._seq += 1
        return Task(
            task_id=f"T{self._seq}",
            agent=agent,
            kind=kind,
            depends_on=depends_on,
            critic_round=critic_round,
            evidence_request_id=evidence_request_id,
            brief=TaskBrief(focus=focus[:500], time_range=time_range),
            created_seq=self._seq,
        )


def _find_cycle(tasks: Iterable[PlannedTask]) -> list[str] | None:
    """A dependency cycle (a self-loop included) as a list of task IDs, or None."""
    deps = {t.task_id: t.depends_on for t in tasks}
    state: dict[str, int] = {}  # 1 = on the current path, 2 = done
    path: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        path.append(node)
        for dep in deps.get(node, ()):
            if state.get(dep) == 1:
                return path[path.index(dep):] + [dep]
            if state.get(dep) is None and dep in deps:
                found = visit(dep)
                if found:
                    return found
        path.pop()
        state[node] = 2
        return None

    for node in deps:
        if state.get(node) is None:
            found = visit(node)
            if found:
                return found
    return None


class TaskGraph:
    def __init__(self, tasks: Iterable[Task] = ()):
        self._tasks: dict[str, Task] = {}
        for task in tasks:
            self.add(task)

    # structure

    def add(self, task: Task) -> None:
        if task.task_id in self._tasks:
            raise ValueError(f"duplicate task ID {task.task_id}")
        missing = [d for d in task.depends_on if d not in self._tasks]
        if missing:
            raise ValueError(f"task {task.task_id} depends on unknown tasks {missing}")
        if task.state is not TaskState.PENDING:
            raise ValueError(f"task {task.task_id} must be added in the pending state")
        self._tasks[task.task_id] = task

    def get(self, task_id: str) -> Task:
        return self._tasks[task_id]

    def __contains__(self, task_id: object) -> bool:
        return task_id in self._tasks

    def __iter__(self) -> Iterator[Task]:
        return iter(tuple(self._tasks.values()))

    def __len__(self) -> int:
        return len(self._tasks)

    def tasks(self) -> tuple[Task, ...]:
        return tuple(self._tasks.values())

    def state(self) -> TaskGraphState:
        return TaskGraphState(tasks=self.tasks())

    # dispatch

    def is_ready(self, task: Task) -> bool:
        return task.state is TaskState.PENDING and all(
            self._tasks[d].state in TERMINAL_TASK_STATES for d in task.depends_on
        )

    def ready(self) -> list[Task]:
        """Pending Tasks whose dependencies are all terminal, in insertion order."""
        return [t for t in self._tasks.values() if self.is_ready(t)]

    def in_state(self, state: TaskState) -> list[Task]:
        return [t for t in self._tasks.values() if t.state is state]

    def running_count(self) -> int:
        return sum(1 for t in self._tasks.values() if t.state is TaskState.RUNNING)

    def transition(self, task_id: str, to: TaskState, **fields) -> Task:
        """Move a Task along an allowed edge, optionally updating other fields; else ``IllegalTransition``."""
        task = self._tasks[task_id]
        if (task.state, to) not in ALLOWED_TRANSITIONS:
            raise IllegalTransition(task_id, task.state, to)
        new = task.model_copy(update={**fields, "state": to})
        self._tasks[task_id] = new
        return new

    def update(self, task_id: str, **fields) -> Task:
        """Change non-state fields (attempt, critic round) of a Task."""
        if "state" in fields:
            raise ValueError("use transition() to change a Task's state")
        new = self._tasks[task_id].model_copy(update=fields)
        self._tasks[task_id] = new
        return new

    # planning

    @staticmethod
    def validate_plan(plan: PlanOutput, max_initial: int) -> PlanDefect | None:
        """The first defect of a planned Task_Graph against Req 6.1 and 6.3, or None if it is usable.

        Specialist Tasks may depend on other Specialist Tasks. The Reasoning Task depends on exactly
        the Specialist Tasks, the Critic Task on exactly the Reasoning Task, and the Reporting Task on
        nothing (it is gated on loop end instead).
        """
        tasks = plan.tasks
        ids = [t.task_id for t in tasks]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            return PlanDefect("duplicate_task_id", f"duplicate task IDs {duplicates}")
        known = set(ids)
        for t in tasks:
            unknown = [d for d in t.depends_on if d not in known]
            if unknown:
                return PlanDefect("unknown_dependency", f"task {t.task_id} depends on unknown tasks {unknown}")
        cycle = _find_cycle(tasks)
        if cycle:
            return PlanDefect("cycle", "dependency cycle " + " -> ".join(cycle))

        by_agent: dict[AgentName, list[PlannedTask]] = {agent: [] for agent in AgentName}
        for t in tasks:
            by_agent[t.agent].append(t)
        if by_agent[AgentName.ORCHESTRATOR]:
            return PlanDefect("structure", "the orchestrator cannot be assigned a task")
        for agent in SPECIALISTS:
            n = len(by_agent[agent])
            if not 1 <= n <= max_initial:
                return PlanDefect("structure", f"{n} {agent} tasks; expected 1 to {max_initial}")
        for agent in (AgentName.REASONING, AgentName.CRITIC, AgentName.REPORTING):
            if len(by_agent[agent]) != 1:
                return PlanDefect("structure", f"{len(by_agent[agent])} {agent} tasks; expected exactly 1")
        specialist_ids = {t.task_id for agent in SPECIALISTS for t in by_agent[agent]}
        for agent in SPECIALISTS:
            for t in by_agent[agent]:
                if not set(t.depends_on) <= specialist_ids:
                    return PlanDefect("structure", f"specialist task {t.task_id} may depend only on specialist tasks")
        (reasoning,) = by_agent[AgentName.REASONING]
        if set(reasoning.depends_on) != specialist_ids:
            return PlanDefect("structure", "the reasoning task must depend on exactly the specialist tasks")
        (critic,) = by_agent[AgentName.CRITIC]
        if set(critic.depends_on) != {reasoning.task_id}:
            return PlanDefect("structure", "the critic task must depend on exactly the reasoning task")
        (reporting,) = by_agent[AgentName.REPORTING]
        if reporting.depends_on:
            return PlanDefect("structure", "the reporting task must have no dependencies")
        return None

    @classmethod
    def from_plan(cls, plan: PlanOutput, factory: TaskFactory) -> "TaskGraph":
        """Build a validated plan. Tasks get run-unique IDs from ``factory`` in a stable topological
        order (plan order wherever the dependencies allow it); planned IDs only resolve dependencies."""
        pending = list(plan.tasks)
        mapping: dict[str, str] = {}
        graph = cls()
        while pending:
            index = next(i for i, t in enumerate(pending) if all(d in mapping for d in t.depends_on))
            planned = pending.pop(index)
            task = factory.new(
                planned.agent,
                _KIND_FOR_AGENT[planned.agent],
                tuple(dict.fromkeys(mapping[d] for d in planned.depends_on)),
                focus=planned.focus,
                time_range=planned.time_range,
            )
            mapping[planned.task_id] = task.task_id
            graph.add(task)
        return graph

    @classmethod
    def default(cls, factory: TaskFactory) -> "TaskGraph":
        """One Task per Specialist, Reasoning on both, Critic on Reasoning, and Reporting (Req 6.7). No LLM call."""
        observability = factory.new(AgentName.OBSERVABILITY, TaskKind.INITIAL, focus="symptoms and onset in logs and metrics")
        dependency = factory.new(AgentName.DEPENDENCY, TaskKind.INITIAL, focus="recent changes, dependencies and history")
        reasoning = factory.new(AgentName.REASONING, TaskKind.REASONING, (observability.task_id, dependency.task_id))
        critic = factory.new(AgentName.CRITIC, TaskKind.CRITIC, (reasoning.task_id,))
        reporting = factory.new(AgentName.REPORTING, TaskKind.REPORTING)
        return cls([observability, dependency, reasoning, critic, reporting])
