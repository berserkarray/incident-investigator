"""Fault_Template interface (Req 22.12). A template produces the fault records of one fault kind."""

from dataclasses import dataclass, field
from typing import ClassVar, Protocol

from pydantic import BaseModel

from ...models.answer_key import ExpectedDataGap, RootCauseLabel
from ...models.scenario import FaultTemplateName, NoiseConstraints
from ..context import GenContext, ProvisionalChange, ProvisionalLog, ProvisionalPastIncident


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
    protected: set[str] = field(default_factory=set)  # components noise must keep clean when constrained


class FaultTemplate(Protocol):
    name: ClassVar[FaultTemplateName]
    params_model: ClassVar[type[BaseModel]]

    def generate(self, ctx: GenContext, params: BaseModel) -> FaultResult: ...


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
