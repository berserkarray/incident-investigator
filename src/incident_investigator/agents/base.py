"""The Agent interface. Agents return outcomes; the Orchestrator commits them after citation validation."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from pydantic import BaseModel

from ..clock import Clock
from ..llm.base import StructuredLLM
from ..models.config import InvestigatorConfig
from ..models.incident import IncidentReport
from ..models.tasks import Task
from ..models.vocab import AgentName
from ..state.ports import BlackboardReader
from ..tools.ports import ToolPort


@dataclass(frozen=True)
class AgentContext:
    board: BlackboardReader
    llm: StructuredLLM
    tools: ToolPort | None  # None for Reasoning, Critic and Reporting (Req 10.6, 12.1)
    clock: Clock
    config: InvestigatorConfig
    incident: IncidentReport
    components: frozenset[str]
    is_first_task: bool  # first Task of this agent in the Run


@dataclass(frozen=True)
class AgentOutcome:
    """A validated agent output plus bookkeeping. ``output`` is the agent's Pydantic output model."""

    task_id: str
    agent: AgentName
    output: BaseModel
    tool_requests: int = 0


class Agent(ABC):
    name: ClassVar[AgentName]

    @abstractmethod
    async def run(self, task: Task, ctx: AgentContext) -> AgentOutcome: ...
