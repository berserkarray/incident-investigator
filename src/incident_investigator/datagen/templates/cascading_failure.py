"""An upstream service runs out of workers; its callers log far more errors than it does (Req 23.12)."""

from typing import ClassVar

from pydantic import BaseModel, Field

from ...models.scenario import FaultTemplateName
from ...models.vocab import LogLevel, MetricName
from ..context import GenContext
from .base import FaultResult, TemplateParams, label, minute_range, pulse, root_component, spaced_logs


class CascadingFailureParams(TemplateParams):
    lead_minutes: int = Field(3, ge=1, le=30)
    root_error_logs: int = Field(6, ge=1)
    caller_error_logs: int = Field(40, ge=1)
    edge_error_logs: int = Field(15, ge=0)


class CascadingFailure:
    name: ClassVar[FaultTemplateName] = FaultTemplateName.CASCADING_FAILURE
    params_model: ClassVar[type[BaseModel]] = CascadingFailureParams

    def generate(self, ctx: GenContext, params: BaseModel) -> FaultResult:
        p = CascadingFailureParams.model_validate(params.model_dump())
        root = root_component(ctx)
        callers = ctx.topology.downstream_of(root)
        if not callers:
            raise ValueError(f"no service calls {root}")
        caller = ctx.affected if ctx.affected in callers else callers[0]
        rng = ctx.rng("fault")
        onset = ctx.onset_minute
        result = FaultResult()

        start, end, lead = onset - p.lead_minutes, ctx.minutes, p.lead_minutes
        cpu = ctx.get_series(root, MetricName.CPU_UTILIZATION)
        pulse(cpu, start, end, 0.98, rise=lead, fall=None, rng=rng, jitter=0.01)
        latency = ctx.get_series(root, MetricName.LATENCY_P99_MS)
        pulse(latency, start, end, lambda v: v * 12, rise=lead, fall=None, rng=rng)
        pulse(ctx.get_series(root, MetricName.ERROR_RATE), onset, ctx.minutes, 0.1, fall=None, rng=rng)
        pulse(ctx.get_series(caller, MetricName.ERROR_RATE), onset, ctx.minutes, 0.4, fall=None, rng=rng)

        exhausted = spaced_logs(
            ctx,
            rng,
            root,
            LogLevel.ERROR,
            "worker pool exhausted: 256/256 threads busy; rejecting request",
            start=onset - 2,
            count=p.root_error_logs,
            every=4,
        )
        result.logs += exhausted
        result.logs += spaced_logs(
            ctx,
            rng,
            caller,
            LogLevel.ERROR,
            f"call to {root} timed out after 2000 ms",
            start=onset + 0.1,
            count=p.caller_error_logs,
            every=0.7,
        )
        for edge in ctx.topology.downstream_of(caller):
            result.logs += spaced_logs(
                ctx,
                rng,
                edge,
                LogLevel.ERROR,
                f"upstream {caller} timed out (504)",
                start=onset + 2,
                count=p.edge_error_logs,
                every=1.5,
            )
            if MetricName.ERROR_RATE in ctx.series_keys(edge):
                pulse(ctx.get_series(edge, MetricName.ERROR_RATE), onset + 2, ctx.minutes, 0.2, fall=None, rng=rng)

        result.key_evidence = [
            *(e.pid for e in exhausted[:3]),
            *(ctx.metric_pid(root, MetricName.CPU_UTILIZATION, m) for m in minute_range(ctx, onset - 2, onset + 1)),
            *(ctx.metric_pid(root, MetricName.LATENCY_P99_MS, m) for m in minute_range(ctx, onset - 1, onset + 1)),
        ]
        result.root_cause = label(ctx, root)
        result.contributing_factors = [f"callers of {root} retry without backoff"]
        return result
