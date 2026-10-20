"""Scheduler: event-driven dispatch of Ready_Tasks (Req 6.2, 6.5, 6.6, 7.1-7.5, 16.1, 16.2, 16.5, 16.6).

``pump()`` runs when Tasks are added and after every terminal transition. It starts Ready_Tasks in
insertion order while fewer than ``concurrency_limit`` Tasks are running, so the pending-to-running
sequence is a topological order. The Reporting Task starts only after ``release_reporting()``, which
the Orchestrator calls once the loop has ended and it has chosen the Reporting path.

Each attempt runs under ``clock.wait_for(..., per_task_timeout_s)``. An Agent_Failure (exception,
per-Task timeout or structured-output failure) gets one retry with the Task still running; a second
failure marks it failed and records a Data_Gap. A failure never cancels other Tasks. ``RunClosed`` and
``BudgetBlocked`` are cancellations, not failures. Transitions are recorded synchronously, so no two
can interleave and the trace shows every one.
"""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from typing import Any, Protocol

from ..agents.base import Agent, AgentContext, AgentOutcome
from ..clock import Clock, ClockTimeout
from ..errors import BudgetBlocked, RunClosed
from ..llm.validator import StructuredOutputFailure
from ..models.gaps import DataGapDraft
from ..models.tasks import Task
from ..models.vocab import TERMINAL_TASK_STATES, FailureKind, TaskKind, TaskState
from ..state.blackboard import Blackboard
from ..tracing import TraceRecorder
from .graph import TaskGraph


class SchedulerHooks(Protocol):
    """What the Orchestrator supplies: agents, contexts, commit logic and loop signals."""

    def agent_for(self, task: Task) -> Agent: ...

    def context_for(self, task: Task) -> AgentContext: ...

    def admit(self, task: Task) -> bool:
        """Called for every Ready_Task before it starts. False cancels it instead (Req 16.7)."""

    def on_rejected(self, task: Task) -> None:
        """After a Task refused by ``admit`` has been cancelled."""

    def on_start(self, task: Task) -> dict[str, Any]:
        """Extra Task fields to set on the pending-to-running transition (for example the Critic_Round)."""

    async def on_success(self, task: Task, outcome: AgentOutcome) -> None:
        """Commit a validated outcome. Raising fails the attempt."""

    async def after_terminal(self, task: Task) -> None:
        """After any terminal transition made by the scheduler's own runners or by ``admit``."""


def failure_kind(exc: BaseException) -> FailureKind:
    if isinstance(exc, ClockTimeout):
        return FailureKind.TASK_TIMEOUT
    if isinstance(exc, StructuredOutputFailure):
        return FailureKind.STRUCTURED_OUTPUT
    return FailureKind.EXCEPTION


class Scheduler:
    def __init__(
        self,
        graph: TaskGraph,
        *,
        hooks: SchedulerHooks,
        board: Blackboard,
        trace: TraceRecorder,
        clock: Clock,
        concurrency_limit: int,
        per_task_timeout_s: float,
        loop_ended: Callable[[], bool],
    ):
        self.graph = graph
        self._hooks = hooks
        self._board = board
        self._trace = trace
        self._clock = clock
        self._limit = concurrency_limit
        self._timeout = per_task_timeout_s
        self._loop_ended = loop_ended
        self._handles: dict[str, asyncio.Task] = {}
        self._terminal: dict[str, asyncio.Event] = {}
        self._reporting_released = False
        self._pumping = False
        self._repump = False

    # graph changes

    def add(self, task: Task, *, pump: bool = True) -> Task:
        self.graph.add(task)
        self._board.put_task(task)
        if pump:
            self.pump()
        return task

    def extend(self, tasks: list[Task]) -> None:
        """Add a whole graph before dispatching any of it (Req 6.1), then dispatch."""
        for task in tasks:
            self.add(task, pump=False)
        self.pump()

    def _transition(self, task_id: str, to: TaskState, **fields: Any) -> Task:
        before = self.graph.get(task_id)
        after = self.graph.transition(task_id, to, **fields)
        self._board.put_task(after)
        self._trace.record(
            "task_transition", task_id=task_id, agent=after.agent, from_state=before.state, to_state=to
        )
        if to in TERMINAL_TASK_STATES:
            self._event(task_id).set()
        return after

    def _event(self, task_id: str) -> asyncio.Event:
        return self._terminal.setdefault(task_id, asyncio.Event())

    # dispatch

    @staticmethod
    def is_reporting(task: Task) -> bool:
        return task.kind is TaskKind.REPORTING

    def pump(self) -> None:
        """Start Ready_Tasks until the concurrency limit is reached or none remains (Req 7.1)."""
        if self._pumping:
            self._repump = True
            return
        self._pumping = True
        try:
            while True:
                self._repump = False
                for candidate in self.graph.ready():
                    task = self.graph.get(candidate.task_id)
                    if not self.graph.is_ready(task):
                        continue  # changed by a hook earlier in this pass
                    if self.is_reporting(task):
                        if not self._reporting_released:
                            continue
                    elif self._loop_ended():
                        continue
                    if not self._hooks.admit(task):
                        self._transition(task.task_id, TaskState.CANCELLED)
                        self._hooks.on_rejected(task)
                        continue
                    if self.graph.running_count() >= self._limit:
                        continue
                    self._start(task)
                if not self._repump:
                    break
        finally:
            self._pumping = False

    def _start(self, task: Task) -> None:
        fields = self._hooks.on_start(task)
        started = self._transition(task.task_id, TaskState.RUNNING, attempt=1, **fields)
        self._handles[task.task_id] = asyncio.get_running_loop().create_task(
            self._run(started.task_id), name=f"agent-task-{started.task_id}"
        )

    def release_reporting(self) -> None:
        self._reporting_released = True
        self.pump()

    # cancellation

    def cancel_all(self, *, include_reporting: bool) -> None:
        """Cancel every pending and running Task (Req 18.5, 18.8); Reporting only when asked."""
        for task in self.graph:
            if self.is_reporting(task) and not include_reporting:
                continue
            if task.state is TaskState.PENDING:
                self._transition(task.task_id, TaskState.CANCELLED)
            elif task.state is TaskState.RUNNING:
                self._transition(task.task_id, TaskState.CANCELLED)
                handle = self._handles.get(task.task_id)
                if handle is not None and not handle.done():
                    handle.cancel()

    def cancel_pending(self, task_id: str) -> None:
        if self.graph.get(task_id).state is TaskState.PENDING:
            self._transition(task_id, TaskState.CANCELLED)

    async def wait_terminal(self, task_id: str) -> Task:
        await self._event(task_id).wait()
        return self.graph.get(task_id)

    async def shutdown(self) -> None:
        """Cancel and reap every runner still alive (end of Run cleanup)."""
        current = asyncio.current_task()
        pending = [h for h in self._handles.values() if not h.done() and h is not current]
        for handle in pending:
            handle.cancel()
        for handle in pending:
            with suppress(asyncio.CancelledError, Exception):
                await handle

    # one Task, up to two attempts

    def _cancelled(self, task_id: str) -> bool:
        return self.graph.get(task_id).state is TaskState.CANCELLED

    async def _after(self, task_id: str) -> None:
        await self._hooks.after_terminal(self.graph.get(task_id))
        self.pump()

    async def _run(self, task_id: str) -> None:
        task = self.graph.get(task_id)
        agent = self._hooks.agent_for(task)
        context = self._hooks.context_for(task)
        for attempt in (1, 2):
            if attempt == 2:
                task = self.graph.update(task_id, attempt=2)
                self._board.put_task(task)
            try:
                outcome = await self._clock.wait_for(agent.run(task, context), self._timeout)
                if self._cancelled(task_id):
                    return
                await self._hooks.on_success(task, outcome)
            except (RunClosed, BudgetBlocked):
                if self.graph.get(task_id).state is TaskState.RUNNING:
                    self._transition(task_id, TaskState.CANCELLED)
                    await self._after(task_id)
                return
            except Exception as exc:  # an Agent_Failure of this attempt (Req 16.1, 16.6)
                if self._cancelled(task_id):
                    return
                kind = failure_kind(exc)
                self._trace.record(
                    "agent_failure",
                    agent=task.agent,
                    task_id=task_id,
                    attempt=attempt,
                    failure_kind=kind,
                    message=f"{type(exc).__name__}: {exc}"[:2000],
                )
                if attempt == 1:
                    continue
                self._transition(task_id, TaskState.FAILED)
                await self._board.add_gap(
                    DataGapDraft(
                        source=task.agent,
                        task_id=task_id,
                        reason=kind.value,
                        affected_time_range=task.brief.time_range,
                        after_verdict=self._board.verdict is not None,
                    )
                )
                await self._after(task_id)
                return
            if self._cancelled(task_id):
                return
            self._transition(task_id, TaskState.COMPLETED)
            await self._after(task_id)
            return
