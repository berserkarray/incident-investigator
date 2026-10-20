"""Evidence_Requests filed by the Critic_Agent (Req 11)."""

from enum import StrEnum, auto

from .base import StrictModel
from .tools import ToolInput
from .vocab import AgentName, ConcernCategory, ToolName


class EvidenceRequestStatus(StrEnum):
    TASKED = auto()
    DUPLICATE = auto()
    COMPLETED = auto()
    FAILED = auto()
    CANCELLED = auto()
    UNFULFILLED = auto()


class EvidenceRequest(StrictModel):
    request_id: str  # "ER{round}.{i}"
    critic_round: int
    target: AgentName
    hypothesis_id: str
    concern: ConcernCategory
    question: str
    suggested_tool: ToolName
    suggested_params: ToolInput
    rationale: str
    status: EvidenceRequestStatus
    task_id: str | None = None
    duplicate_of: str | None = None  # matching Task ID or earlier request ID
