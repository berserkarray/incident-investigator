"""Provider-agnostic LLM interface (Req 5.1-5.3).

Every Agent and the Baseline_Agent reach a model only through an ``LLMClient`` wrapped by the
LLM gateway; adapters exist for OpenAI and the Fake_LLM (Anthropic is optional).
"""

from enum import StrEnum, auto
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel, Field

from ..models.base import JsonValue, StrictModel
from ..models.budget import TokenCount
from ..models.evidence import LedgerView
from ..models.hypothesis import HypothesisRevision
from ..models.vocab import Caller, CallKind, ToolName

M = TypeVar("M", bound=BaseModel)

Provider = Literal["anthropic", "openai", "fake"]


class LLMRequest(StrictModel):
    caller: Caller
    call_kind: CallKind
    model: str
    system: str  # Prompt_Template text
    user: str  # run-time content, including the <context> JSON block
    output_schema: dict[str, JsonValue] = {}
    max_output_tokens: int = Field(4096, ge=1)
    temperature: float | None = 0.0  # None when the model does not accept the parameter


class LLMResponse(StrictModel):
    text: str
    usage: TokenCount
    provider: Provider
    duration_s: float = Field(0.0, ge=0)


class LLMErrorCategory(StrEnum):
    RATE_LIMIT = auto()
    SERVER = auto()
    TIMEOUT = auto()
    AUTH = auto()
    BAD_REQUEST = auto()
    OTHER = auto()


RETRYABLE_CATEGORIES: frozenset[LLMErrorCategory] = frozenset(
    {LLMErrorCategory.RATE_LIMIT, LLMErrorCategory.SERVER, LLMErrorCategory.TIMEOUT}
)


class LLMCallError(Exception):
    """An LLM call that failed after its permitted attempts. The message is already redacted."""

    def __init__(self, category: LLMErrorCategory, message: str, attempts: int = 1):
        super().__init__(f"{category}: {message}")
        self.category = category
        self.attempts = attempts


class LLMClient(Protocol):
    async def complete(self, request: LLMRequest) -> LLMResponse: ...


class ValidationContext(StrictModel):
    """Run-time facts that context validators need (passed as Pydantic ``context=``)."""

    ledger: LedgerView = LedgerView()
    valid_components: frozenset[str] = frozenset()
    incident_claim_ids: tuple[str, ...] = ()
    latest_hypotheses: tuple[HypothesisRevision, ...] = ()  # active set, rank order
    allowed_tools: frozenset[ToolName] | None = None
    max_evidence_requests: int = Field(10, ge=1)
    max_actions: int = Field(6, ge=1)


class StructuredLLM(Protocol):
    """The gateway port Agents use: one validated, structured call with at most one repair."""

    async def structured(
        self,
        caller: Caller,
        kind: CallKind,
        system: str,
        user: str,
        model: type[M],
        ctx: ValidationContext,
    ) -> M: ...
