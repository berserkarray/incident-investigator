"""An external API starts failing; its caller and the affected service degrade with no change anywhere (Req 23.3)."""

from typing import ClassVar

from pydantic import BaseModel, Field

from ...models.scenario import FaultTemplateName, NoiseConstraints
from ...models.vocab import LogLevel, MetricName
from ..context import GenContext
from .base import FaultResult, TemplateParams, label, minute_range, pulse, root_component, spaced_logs


class ThirdPartyOutageParams(TemplateParams):
    outage_minutes: int = Field(50, ge=10)
    status_code: int = Field(503, ge=500, le=599)
    error_rate: float = Field(0.6, gt=0, le=1)
    caller_error_logs: int = Field(30, ge=3)


class ThirdPartyOutage:
    name: ClassVar[FaultTemplateName] = FaultTemplateName.THIRD_PARTY_OUTAGE
    params_model: ClassVar[type[BaseModel]] = ThirdPartyOutageParams

    def generate(self, ctx: GenContext, params: BaseModel) -> FaultResult:
        p = ThirdPartyOutageParams.model_validate(params.model_dump())
        provider = root_component(ctx)
        callers = ctx.topology.downstream_of(provider)
        if not callers:
            raise ValueError(f"no service calls {provider}")
        caller = callers[0]
        rng = ctx.rng("fault")
        onset = ctx.onset_minute
        end = onset + p.outage_minutes
        result = FaultResult(noise_constraints=NoiseConstraints(no_changes_in_window=True))

        pulse(ctx.get_series(provider, MetricName.ERROR_RATE), onset, end, p.error_rate, fall=4, rng=rng)
        pulse(ctx.get_series(provider, MetricName.LATENCY_P99_MS), onset, end, lambda v: v * 20, fall=4, rng=rng)
        pulse(ctx.get_series(caller, MetricName.ERROR_RATE), onset, end, p.error_rate / 2, fall=4, rng=rng)
        pulse(ctx.get_series(caller, MetricName.LATENCY_P99_MS), onset, end, lambda v: v * 4, fall=4, rng=rng)

        messages = (
            f"call to {provider} failed: HTTP {p.status_code} Service Unavailable (attempt 3/3)",
            f"timeout after 5000 ms waiting for {provider}",
        )
        errors = spaced_logs(
            ctx, rng, caller, LogLevel.ERROR, lambda i: messages[i % 2], start=onset + 0.1, count=p.caller_error_logs, every=1.3
        )
        result.logs += errors
        for downstream in ctx.topology.downstream_of(caller):
            if MetricName.ERROR_RATE in ctx.series_keys(downstream):
                pulse(ctx.get_series(downstream, MetricName.ERROR_RATE), onset + 1, end, p.error_rate / 5, fall=4, rng=rng)
            result.logs += spaced_logs(
                ctx,
                rng,
                downstream,
                LogLevel.WARN,
                f"dependency {caller} returned 502 for POST /api/v1/orders",
                start=onset + 1,
                count=12,
                every=2.5,
            )

        result.key_evidence = [
            *(e.pid for e in errors[:3]),
            *(ctx.metric_pid(provider, MetricName.ERROR_RATE, m) for m in minute_range(ctx, onset, onset + 5)),
        ]
        result.root_cause = label(ctx, provider)
        result.contributing_factors = [f"no fallback when {provider} is unavailable"]
        return result
