"""Task_Graph basics: Task creation and the Default_Task_Graph (Req 6.7).

Plan validation, Ready_Task dispatch and allowed-transition enforcement are added with the scheduler.
"""

from ..models.tasks import Task, TaskBrief
from ..models.vocab import AgentName, TaskKind


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
    ) -> Task:
        self._seq += 1
        return Task(
            task_id=f"T{self._seq}",
            agent=agent,
            kind=kind,
            depends_on=depends_on,
            critic_round=critic_round,
            evidence_request_id=evidence_request_id,
            brief=TaskBrief(focus=focus[:500]),
            created_seq=self._seq,
        )


def default_task_graph(factory: TaskFactory) -> list[Task]:
    """One Task per Specialist, Reasoning on both, Critic on Reasoning, and Reporting. Built without an LLM call."""
    observability = factory.new(AgentName.OBSERVABILITY, TaskKind.INITIAL, focus="symptoms and onset in logs and metrics")
    dependency = factory.new(AgentName.DEPENDENCY, TaskKind.INITIAL, focus="recent changes, dependencies and history")
    reasoning = factory.new(AgentName.REASONING, TaskKind.REASONING, (observability.task_id, dependency.task_id))
    critic = factory.new(AgentName.CRITIC, TaskKind.CRITIC, (reasoning.task_id,), critic_round=1)
    reporting = factory.new(AgentName.REPORTING, TaskKind.REPORTING)
    return [observability, dependency, reasoning, critic, reporting]
