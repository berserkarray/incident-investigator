"""The Tool_Layer pipeline: validate -> Circuit_Breaker -> Tool_Call Budget -> call -> summarise -> append.

Rejection precedence follows design D1. The breaker and budget checks run on every attempt, so a
retry can itself be rejected. Only the Tool_Layer writes Evidence_Items and tool Data_Gaps.
"""

import time
from collections.abc import Mapping
from enum import StrEnum, auto
from typing import Protocol

from pydantic import ValidationError

from ..clock import Clock
from ..errors import RunClosed
from ..models.base import JsonValue, TimeRange
from ..models.evidence import Observation, ObservationSummary
from ..models.gaps import DataGapDraft
from ..models.snapshot import IncidentSnapshot
from ..models.tools import (
    TOOL_INPUT_MODELS,
    ToolError,
    ToolInputBase,
    ToolRecord,
    ToolResult,
    input_service,
    input_time_range,
)
from ..models.vocab import Caller, ErrorCode, Reliability, TerminationReason, ToolName
from ..tracing import TraceRecorder
from .impl import TOOL_REGISTRY, ToolFn, cut_to_limit
from .ports import EvidenceSink, GapSink, TerminationSignal, ToolBudget
from .summary import ObservationSummarizer


class Admission(StrEnum):
    NORMAL = auto()
    TRIAL = auto()
    REJECT = auto()


class Breaker(Protocol):
    def admit(self, tool: ToolName, now: float) -> Admission: ...

    def record(self, tool: ToolName, admission: Admission, timed_out: bool, now: float) -> None: ...

    def release_trial(self, tool: ToolName) -> None: ...


class AlwaysClosedBreaker:
    """Placeholder breaker for the thin slice; the real Circuit_Breaker arrives in task 5.4."""

    def admit(self, tool: ToolName, now: float) -> Admission:
        return Admission.NORMAL

    def record(self, tool: ToolName, admission: Admission, timed_out: bool, now: float) -> None:
        return None

    def release_trial(self, tool: ToolName) -> None:
        return None


class Injector(Protocol):
    def apply(
        self, tool: ToolName, inp: ToolInputBase, attempt: int, matches: list[ToolRecord]
    ) -> ToolResult: ...


class NoFailureInjector:
    """Placeholder injector: never injects; applies the record limit (Req 13.9)."""

    def apply(self, tool: ToolName, inp: ToolInputBase, attempt: int, matches: list[ToolRecord]) -> ToolResult:
        records, truncated = cut_to_limit(matches, inp.limit)
        return ToolResult(tool=tool, records=records, truncated=truncated)


class RetryDelays(Protocol):
    max_attempts: int

    def delay(self, tool: ToolName, inp: ToolInputBase, retry_no: int) -> float: ...


class NoRetry:
    max_attempts = 1

    def delay(self, tool: ToolName, inp: ToolInputBase, retry_no: int) -> float:
        return 0.0


class BoardToolBudget:
    """Tool_Call Budget backed by the Blackboard usage counter."""

    def __init__(self, board, limit: int):
        self._board = board
        self.limit = limit

    def try_reserve(self) -> bool:
        return self._board.reserve_tool_call(self.limit)

    @property
    def used(self) -> int:
        return self._board.usage.tool_calls


def _reliability(result: ToolResult) -> Reliability:
    if result.partial:
        return Reliability.PARTIAL
    if result.stale:
        return Reliability.STALE
    return Reliability.COMPLETE


def _evidence_time_range(inp: ToolInputBase, result: ToolResult) -> TimeRange | None:
    time_range = input_time_range(inp)
    if time_range is not None and result.stale and result.as_of is not None and result.as_of > time_range.start:
        return TimeRange(start=time_range.start, end=result.as_of)
    return time_range


class ToolLayer:
    def __init__(
        self,
        *,
        snapshot: IncidentSnapshot,
        ledger: EvidenceSink,
        gaps: GapSink,
        budget: ToolBudget,
        trace: TraceRecorder,
        clock: Clock,
        summarizer: ObservationSummarizer,
        termination: TerminationSignal | None = None,
        injector: Injector | None = None,
        breaker: Breaker | None = None,
        retry: RetryDelays | None = None,
        registry: Mapping[ToolName, ToolFn] | None = None,
    ):
        self._snapshot = snapshot
        self._components = snapshot.component_names
        self._ledger = ledger
        self._gaps = gaps
        self._budget = budget
        self._trace = trace
        self._clock = clock
        self._summarizer = summarizer
        self._termination = termination
        self._injector = injector or NoFailureInjector()
        self._breaker = breaker or AlwaysClosedBreaker()
        self._retry = retry or NoRetry()
        self._registry = dict(registry or TOOL_REGISTRY)
        self._closed = False

    @property
    def components(self) -> frozenset[str]:
        return self._components

    @property
    def available_tools(self) -> frozenset[ToolName]:
        return frozenset(self._registry)

    def close(self) -> None:
        self._closed = True

    # pipeline

    async def request(
        self,
        caller: Caller,
        task_id: str | None,
        tool: str,
        params: Mapping[str, JsonValue],
        allowed: frozenset[ToolName] | None = None,
    ) -> Observation:
        if self._closed:
            raise RunClosed("the Run has ended")
        raw = {k: v for k, v in dict(params).items() if k != "tool"}

        # 1. validation (Req 13.8): no Tool_Call, no budget, no gap, no breaker change
        try:
            tool_name = ToolName(tool)
        except ValueError:
            return self._invalid(caller, task_id, tool, raw, f"unknown tool {tool!r}", ("tool",))
        if allowed is not None and tool_name not in allowed:
            return self._invalid(caller, task_id, tool_name, raw, f"{tool_name} is not available to {caller}", ("tool",))
        if tool_name not in self._registry:
            return self._invalid(caller, task_id, tool_name, raw, f"{tool_name} is not available in this build", ("tool",))
        try:
            inp = TOOL_INPUT_MODELS[tool_name].model_validate({**raw, "tool": tool_name.value})
        except ValidationError as exc:
            fields = tuple(".".join(str(p) for p in e["loc"]) for e in exc.errors(include_url=False))
            return self._invalid(caller, task_id, tool_name, raw, "invalid tool input", fields)
        service = input_service(inp)
        if service is not None and service not in self._components:
            return self._invalid(caller, task_id, tool_name, raw, f"unknown service {service!r}", ("service",))

        fn = self._registry[tool_name]
        for attempt in range(1, self._retry.max_attempts + 1):
            now = self._clock.monotonic()
            # 2. Circuit_Breaker
            admission = self._breaker.admit(tool_name, now)
            if admission is Admission.REJECT:
                return await self._rejected(caller, task_id, tool_name, inp, raw, ErrorCode.CIRCUIT_OPEN)
            # 3. Tool_Call Budget
            if not self._budget.try_reserve():
                if admission is Admission.TRIAL:
                    self._breaker.release_trial(tool_name)
                if self._termination is not None:
                    self._termination.signal(TerminationReason.TOOL_CALL_BUDGET_EXHAUSTED)
                return await self._rejected(caller, task_id, tool_name, inp, raw, ErrorCode.BUDGET_EXHAUSTED)
            # 4. the call itself
            started = time.perf_counter()
            result = self._injector.apply(tool_name, inp, attempt, fn(inp, self._snapshot))
            timed_out = result.error is not None and result.error.code is ErrorCode.TIMEOUT
            self._trace.record(
                "tool_call",
                caller=caller,
                task_id=task_id,
                tool=tool_name,
                input=inp.model_dump(mode="json"),
                attempt=attempt,
                trial=admission is Admission.TRIAL,
                error_code=result.error.code if result.error else None,
                partial=result.partial,
                stale=result.stale,
                truncated=result.truncated,
                duration_s=time.perf_counter() - started,
            )
            self._breaker.record(tool_name, admission, timed_out, self._clock.monotonic())
            if timed_out:
                if attempt < self._retry.max_attempts:
                    await self._clock.sleep(self._retry.delay(tool_name, inp, attempt))
                    continue
                await self._gap(tool_name, inp, ErrorCode.TIMEOUT)
                return self._error_observation(tool_name, inp, raw, result.error)
            # 5. success: summarise and append exactly one Evidence_Item (Req 3.2-3.6)
            summary = self._summarizer.summarize(result)
            item = await self._ledger.append_evidence(
                tool=tool_name,
                query=inp,
                summary=summary,
                time_range=_evidence_time_range(inp, result),
                reliability=_reliability(result),
                records=result.records,
                produced_by=caller,
                task_id=task_id,
            )
            return Observation(
                evidence_id=item.evidence_id,
                tool=tool_name,
                query=inp,
                raw_params=raw,
                summary=summary,
                records=result.records,
                partial=result.partial,
                stale=result.stale,
                as_of=result.as_of,
                truncated=result.truncated,
            )
        raise AssertionError("unreachable: the retry loop always returns")

    # helpers

    def _invalid(self, caller, task_id, tool, raw, message, fields) -> Observation:
        error = ToolError(code=ErrorCode.INVALID_INPUT, message=message, invalid_fields=tuple(fields))
        self._trace.record(
            "tool_rejected",
            caller=caller,
            task_id=task_id,
            tool=str(tool),
            input=raw,
            code=ErrorCode.INVALID_INPUT,
            invalid_fields=tuple(fields),
        )
        return Observation(
            evidence_id=None,
            tool=tool,
            query=None,
            raw_params=raw,
            summary=ObservationSummary(record_count=0, note=ErrorCode.INVALID_INPUT.value),
            error=error,
        )

    async def _rejected(self, caller, task_id, tool, inp, raw, code: ErrorCode) -> Observation:
        self._trace.record(
            "tool_rejected",
            caller=caller,
            task_id=task_id,
            tool=str(tool),
            input=inp.model_dump(mode="json"),
            code=code,
        )
        await self._gap(tool, inp, code)
        message = "circuit breaker is open" if code is ErrorCode.CIRCUIT_OPEN else "tool-call budget exhausted"
        return self._error_observation(tool, inp, raw, ToolError(code=code, message=message))

    async def _gap(self, tool: ToolName, inp: ToolInputBase, code: ErrorCode) -> None:
        await self._gaps.add_gap(
            DataGapDraft(source=tool, query=inp, reason=code.value, affected_time_range=input_time_range(inp))
        )

    @staticmethod
    def _error_observation(tool, inp, raw, error: ToolError) -> Observation:
        return Observation(
            evidence_id=None,
            tool=tool,
            query=inp,
            raw_params=raw,
            summary=ObservationSummary(record_count=0, note=error.code.value),
            error=error,
        )


class ScopedToolPort:
    """A caller's view of the Tool_Layer, restricted to its own Tools (Req 8.1, 9.1)."""

    def __init__(self, layer: ToolLayer, caller: Caller, allowed: frozenset[ToolName] | None):
        self._layer = layer
        self.caller = caller
        self.allowed = allowed

    async def request(self, task_id: str | None, tool: ToolName | str, params: Mapping[str, JsonValue]) -> Observation:
        return await self._layer.request(self.caller, task_id, str(tool), params, allowed=self.allowed)
