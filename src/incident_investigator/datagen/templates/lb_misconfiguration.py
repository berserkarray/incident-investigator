"""The entry load balancer health-checks the affected pool on the wrong port and drops it (Req 23.7)."""

from datetime import timedelta
from typing import ClassVar

from pydantic import BaseModel, Field

from ...models.scenario import FaultTemplateName
from ...models.vocab import ChangeType, LogLevel, MetricName
from ..context import GenContext
from .base import FaultResult, TemplateParams, label, minute_range, pulse, root_component, spaced_logs


class LBMisconfigurationParams(TemplateParams):
    change_lead_minutes: int = Field(8, ge=1, le=60)
    configured_port: int = Field(8081, ge=1, le=65535)
    service_port: int = Field(8080, ge=1, le=65535)
    error_rate: float = Field(0.55, gt=0, le=1)
    error_logs: int = Field(30, ge=3)


class LBMisconfiguration:
    name: ClassVar[FaultTemplateName] = FaultTemplateName.LB_MISCONFIGURATION
    params_model: ClassVar[type[BaseModel]] = LBMisconfigurationParams

    def generate(self, ctx: GenContext, params: BaseModel) -> FaultResult:
        p = LBMisconfigurationParams.model_validate(params.model_dump())
        lb = root_component(ctx)
        affected = ctx.affected
        rng = ctx.rng("fault")
        onset = ctx.onset_minute
        result = FaultResult()

        change = ctx.change(
            "fault",
            ctx.onset - timedelta(minutes=p.change_lead_minutes),
            lb,
            ChangeType.CONFIG_CHANGE,
            f"Update backend pool {affected}: health check port {p.service_port} -> {p.configured_port}",
            setting="pool.health_check.port",
            pool=affected,
        )
        result.changes.append(change)
        errors = spaced_logs(
            ctx,
            rng,
            lb,
            LogLevel.ERROR,
            f"no healthy backends in pool {affected}; returned 503 to client",
            start=onset + 0.1,
            count=p.error_logs,
            every=1.0,
        )
        probes = spaced_logs(
            ctx,
            rng,
            lb,
            LogLevel.WARN,
            f"health check GET /healthz on {affected}:{p.configured_port} failed: connection refused",
            start=onset,
            count=6,
            every=3,
        )
        result.logs += errors + probes

        pulse(ctx.get_series(lb, MetricName.ERROR_RATE), onset, ctx.minutes, p.error_rate, fall=None, rng=rng)
        pulse(ctx.get_series(affected, MetricName.REQUEST_RATE), onset, ctx.minutes, lambda v: v * 0.1, fall=None, rng=rng)

        result.key_evidence = [
            change.pid,
            probes[0].pid,
            *(e.pid for e in errors[:3]),
            *(ctx.metric_pid(affected, MetricName.REQUEST_RATE, m) for m in minute_range(ctx, onset, onset + 3)),
        ]
        result.root_cause = label(ctx, lb)
        result.contributing_factors = ["health check settings are not validated against the service port"]
        return result
