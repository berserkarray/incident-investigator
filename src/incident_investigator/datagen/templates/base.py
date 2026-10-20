"""Fault_Template interface (Req 22.12). A template produces the fault records of one fault kind.

Templates work on provisional record IDs (see ``GenContext``) and name their Key_Evidence with them.
They declare the ``NoiseConstraints`` their scenario needs; the generator applies the constraints to
noise and red herrings, and the condition checks verify the generated output independently.
"""

import random
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import ClassVar, Protocol

from pydantic import BaseModel, ConfigDict

from ...models.answer_key import ExpectedDataGap, RootCauseLabel
from ...models.scenario import FaultTemplateName, NoiseConstraints
from ...models.vocab import LogLevel
from ..context import GenContext, ProvisionalChange, ProvisionalLog, ProvisionalPastIncident


class TemplateParams(BaseModel):
    """Base for template parameter models: unknown parameters are rejected with their names."""

    model_config = ConfigDict(extra="forbid", frozen=True)


@dataclass
class FaultResult:
    logs: list[ProvisionalLog] = field(default_factory=list)
    changes: list[ProvisionalChange] = field(default_factory=list)
    past_incidents: list[ProvisionalPastIncident] = field(default_factory=list)
    key_evidence: list[str] = field(default_factory=list)  # provisional IDs
    root_cause: RootCauseLabel | None = None
    contributing_factors: list[str] = field(default_factory=list)
    expected_gaps: list[ExpectedDataGap] = field(default_factory=list)
    noise_constraints: NoiseConstraints = field(default_factory=NoiseConstraints)


class FaultTemplate(Protocol):
    name: ClassVar[FaultTemplateName]
    params_model: ClassVar[type[BaseModel]]

    def generate(self, ctx: GenContext, params: BaseModel) -> FaultResult: ...


def root_component(ctx: GenContext) -> str:
    """The Component that plays the spec's root role."""
    if ctx.spec.fault.root_role is None:
        raise ValueError("this template needs fault.root_role")
    return ctx.topology.role(ctx.spec.fault.root_role)


def label(ctx: GenContext, component: str) -> RootCauseLabel:
    assert ctx.spec.fault.cause_type is not None
    return RootCauseLabel(component=component, cause_type=ctx.spec.fault.cause_type)


# metric shaping


def ramp(series: list[float], start: int, end: int, target: float) -> None:
    """Move ``series[start:end]`` linearly from its value at ``start`` to ``target``, then hold."""
    if start >= len(series):
        return
    origin = series[start]
    span = max(1, end - start)
    for i in range(start, len(series)):
        fraction = min(1.0, (i - start) / span)
        series[i] = origin + (target - origin) * fraction


def scale(series: list[float], start: int, end: int, factor: float) -> None:
    for i in range(max(0, start), min(len(series), end)):
        series[i] *= factor


def pulse(
    series: list[float],
    start: int,
    end: int,
    target: float | Callable[[float], float],
    *,
    rise: int = 1,
    fall: int | None = 0,
    rng: random.Random | None = None,
    jitter: float = 0.03,
) -> None:
    """Move ``series`` to ``target`` from minute ``start`` (reaching it after ``rise`` minutes), hold it
    until ``end``, then return to the original values over ``fall`` minutes (``None`` holds to the end).

    ``target`` is a value or a function of the original value (for example ``lambda v: v * 6``).
    With an ``rng``, held values get a small relative jitter so the series never goes flat.
    """
    length = len(series)
    stop = length if fall is None else min(length, end + fall)
    original = list(series)
    for i in range(max(0, start), stop):
        if i < end or fall is None:
            weight = min(1.0, (i - start + 1) / max(1, rise))
        else:
            weight = 1.0 - (i - end + 1) / (fall + 1)
        goal = target(original[i]) if callable(target) else target
        if rng is not None:
            goal *= 1 + rng.uniform(-jitter, jitter)
        series[i] = max(0.0, original[i] + (goal - original[i]) * weight)


# log emission


def spaced_logs(
    ctx: GenContext,
    rng: random.Random,
    service: str,
    level: LogLevel,
    message: str | Callable[[int], str],
    *,
    start: float,
    count: int,
    every: float,
    spread: float = 0.4,
    scope: str = "fault",
    **attributes: str,
) -> list[ProvisionalLog]:
    """``count`` logs from minute ``start``, about ``every`` minutes apart, inside the telemetry window."""
    logs = []
    for i in range(count):
        minute = start + i * every + rng.uniform(0, spread * every)
        if minute < 0:
            continue
        if minute >= ctx.minutes:
            break
        text = message(i) if callable(message) else message
        logs.append(ctx.log(scope, minute, service, level, text, **attributes))
    return logs


def minute_range(ctx: GenContext, start: int, end: int) -> range:
    """Whole minutes ``[start, end)`` clipped to the telemetry window."""
    return range(max(0, start), min(ctx.minutes, end))
