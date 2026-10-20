"""LLMGateway: the only path from Agents to a model (Req 5.3-5.6, 18.3, 18.4).

It gates calls on the Run state and token Budget, records usage per caller on the Blackboard,
traces every call, and runs the Structured_Output_Validator with exactly one repair.
"""

import time
from typing import Protocol, TypeVar

from pydantic import BaseModel

from ..errors import BudgetBlocked, RunClosed
from ..models.base import canonical_json
from ..models.budget import BudgetLimits, BudgetUsage, TokenCount
from ..models.vocab import AgentName, Caller, CallKind, TerminationReason
from ..tracing import TraceRecorder
from .base import LLMCallError, LLMClient, LLMRequest, LLMResponse, SemanticCheck, ValidationContext
from .validator import OutputInvalid, StructuredOutputFailure, parse_output, repair_prompt

M = TypeVar("M", bound=BaseModel)


class UsageSink(Protocol):
    """Implemented by the Blackboard."""

    async def add_usage(self, caller: Caller, tokens: TokenCount) -> BudgetUsage: ...

    @property
    def usage(self) -> BudgetUsage: ...


class LLMGateway:
    def __init__(
        self,
        client: LLMClient,
        usage: UsageSink,
        trace: TraceRecorder,
        limits: BudgetLimits,
        model_name: str,
        termination=None,
    ):
        self._client = client
        self._usage = usage
        self._trace = trace
        self._limits = limits
        self._model = model_name
        self._termination = termination
        self._closed = False
        self._loop_ended = False

    def close(self) -> None:
        """No further LLM call of any caller (wall-clock timeout, Req 18.5)."""
        self._closed = True

    def end_loop(self) -> None:
        """The feedback loop ended: only the Reporting_Agent may still call the model (Req 18.8)."""
        self._loop_ended = True

    def _gate(self, caller: Caller) -> None:
        if self._closed:
            raise RunClosed("the Run has ended")
        if self._loop_ended and caller != AgentName.REPORTING:
            raise RunClosed("the feedback loop has ended")
        used = self._usage.usage.total_tokens
        if used >= self._limits.tokens:
            raise BudgetBlocked("token budget exhausted")
        if caller != AgentName.REPORTING and used >= self._limits.tokens - self._limits.reporting_reserve:
            raise BudgetBlocked("token budget minus the Reporting_Agent reserve is exhausted")

    async def raw(self, request: LLMRequest) -> LLMResponse:
        self._gate(request.caller)
        started = time.perf_counter()
        try:
            response = await self._client.complete(request)
        except LLMCallError as exc:
            self._trace.record(
                "llm_call",
                caller=request.caller,
                call_kind=request.call_kind,
                attempt=exc.attempts,
                input_tokens=0,
                output_tokens=0,
                duration_s=time.perf_counter() - started,
                outcome="error",
                error_category=str(exc.category),
            )
            raise
        usage = await self._usage.add_usage(request.caller, response.usage)
        self._trace.record(
            "llm_call",
            caller=request.caller,
            call_kind=request.call_kind,
            attempt=response.attempts,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            # the provider's own timing for a single attempt; the whole call, backoff included, after retries
            duration_s=response.duration_s if response.attempts == 1 and response.duration_s else time.perf_counter() - started,
            outcome="ok",
            retried_errors=response.retried_errors,
        )
        if self._termination is not None and usage.total_tokens >= self._limits.tokens - self._limits.reporting_reserve:
            self._termination.signal(TerminationReason.TOKEN_BUDGET_EXHAUSTED)
        return response

    async def structured(
        self,
        caller: Caller,
        kind: CallKind,
        system: str,
        user: str,
        model: type[M],
        ctx: ValidationContext,
        check: SemanticCheck | None = None,
    ) -> M:
        schema = model.model_json_schema()
        user = f"{user}\n\nRespond with one JSON object that validates against this JSON Schema:\n{canonical_json(schema)}"
        request = LLMRequest(
            caller=caller,
            call_kind=kind,
            model=self._model,
            system=system,
            user=user,
            output_schema=schema,
        )
        response = await self.raw(request)
        try:
            return parse_output(response.text, model, ctx, check)
        except OutputInvalid as first:
            repair = request.model_copy(
                update={
                    "call_kind": CallKind.REPAIR,
                    "repair_of": kind,
                    "user": repair_prompt(user, response.text, first.errors),
                }
            )
            repaired = await self.raw(repair)
            try:
                return parse_output(repaired.text, model, ctx, check)
            except OutputInvalid as second:
                raise StructuredOutputFailure(second.errors) from None
