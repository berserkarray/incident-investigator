"""Tasks and the Task_Graph state (Req 6)."""

from pydantic import Field

from .base import StrictModel, TimeRange
from .vocab import AgentName, TaskKind, TaskState


class TaskBrief(StrictModel):
    focus: str = Field("", max_length=500)
    time_range: TimeRange | None = None


class Task(StrictModel):
    task_id: str
    agent: AgentName
    kind: TaskKind
    depends_on: tuple[str, ...] = ()
    state: TaskState = TaskState.PENDING
    attempt: int = Field(0, ge=0, le=2)
    critic_round: int | None = None
    evidence_request_id: str | None = None
    brief: TaskBrief = TaskBrief()
    created_seq: int = Field(ge=0)


class TaskGraphState(StrictModel):
    tasks: tuple[Task, ...] = ()  # insertion order
