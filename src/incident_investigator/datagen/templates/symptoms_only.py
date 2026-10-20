"""Symptoms on the affected service and nothing that explains them (Req 23.10)."""

from typing import ClassVar

from pydantic import BaseModel, Field

from ...models.scenario import FaultTemplateName
from ...models.vocab import LogLevel, MetricName
from ..context import GenContext
from .base import FaultResult, TemplateParams, pulse, spaced_logs


class SymptomsOnlyParams(TemplateParams):
    error_rate: float = Field(0.12, gt=0, le=1)
    error_logs: int = Field(20, ge=1)
    latency_factor: float = Field(2.5, ge=1)


class SymptomsOnly:
    name: ClassVar[FaultTemplateName] = FaultTemplateName.SYMPTOMS_ONLY
    params_model: ClassVar[type[BaseModel]] = SymptomsOnlyParams

    def generate(self, ctx: GenContext, params: BaseModel) -> FaultResult:
        p = SymptomsOnlyParams.model_validate(params.model_dump())
        affected = ctx.affected
        rng = ctx.rng("fault")
        onset = ctx.onset_minute
        result = FaultResult()

        messages = ("request failed with status 500", "request failed with status 503")
        result.logs += spaced_logs(
            ctx, rng, affected, LogLevel.ERROR, lambda i: messages[i % 2], start=onset + 0.1, count=p.error_logs, every=1.5
        )
        pulse(ctx.get_series(affected, MetricName.ERROR_RATE), onset, ctx.minutes, p.error_rate, fall=None, rng=rng)
        latency = ctx.get_series(affected, MetricName.LATENCY_P99_MS)
        pulse(latency, onset, ctx.minutes, lambda v: v * p.latency_factor, fall=None, rng=rng)
        return result  # no Root_Cause_Label and no Key_Evidence: the insufficient-evidence marker
