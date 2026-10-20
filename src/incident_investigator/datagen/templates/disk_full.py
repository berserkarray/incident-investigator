"""The primary database's data volume fills up; writes fail at onset (Req 23.8)."""

from typing import ClassVar

from pydantic import BaseModel, Field

from ...models.scenario import FaultTemplateName
from ...models.vocab import LogLevel, MetricName
from ..context import GenContext
from .base import FaultResult, TemplateParams, label, minute_range, pulse, root_component, spaced_logs


class DiskFullParams(TemplateParams):
    fill_minutes: int = Field(150, ge=30)
    warning_utilization: float = Field(0.9, gt=0.5, lt=1.0)
    error_logs: int = Field(30, ge=3)
    error_rate: float = Field(0.3, gt=0, le=1)


class DiskFull:
    name: ClassVar[FaultTemplateName] = FaultTemplateName.DISK_FULL
    params_model: ClassVar[type[BaseModel]] = DiskFullParams

    def generate(self, ctx: GenContext, params: BaseModel) -> FaultResult:
        p = DiskFullParams.model_validate(params.model_dump())
        db = root_component(ctx)
        affected = ctx.affected
        rng = ctx.rng("fault")
        onset = ctx.onset_minute
        result = FaultResult()

        disk = ctx.get_series(db, MetricName.DISK_UTILIZATION)
        start = max(0, onset - p.fill_minutes)
        base = disk[start]
        for i in range(start, min(onset, ctx.minutes)):
            disk[i] = base + (0.995 - base) * (i - start) / max(1, onset - start) + rng.uniform(-0.001, 0.001)
        for i in range(onset, ctx.minutes):
            disk[i] = 0.998 + rng.uniform(0, 0.002)
        crossing = next((i for i in range(start, onset) if disk[i] >= p.warning_utilization), max(0, onset - 5))

        warning = ctx.log(
            "fault",
            crossing + 0.2,
            db,
            LogLevel.WARN,
            f"disk usage on /var/lib/pgdata at {int(disk[crossing] * 100)}% (threshold {int(p.warning_utilization * 100)}%)",
        )
        result.logs.append(warning)
        errors = spaced_logs(
            ctx,
            rng,
            db,
            LogLevel.ERROR,
            lambda i: f'could not extend file "base/16384/{rng.randint(1000, 9999)}": No space left on device',
            start=onset + 0.05,
            count=p.error_logs,
            every=1.3,
        )
        result.logs += errors
        result.logs += spaced_logs(
            ctx,
            rng,
            affected,
            LogLevel.ERROR,
            f"INSERT into orders failed on {db}: could not write to file (No space left on device)",
            start=onset + 0.3,
            count=20,
            every=1.5,
        )
        pulse(ctx.get_series(db, MetricName.LATENCY_P99_MS), onset, ctx.minutes, lambda v: v * 4, fall=None, rng=rng)
        pulse(ctx.get_series(affected, MetricName.ERROR_RATE), onset, ctx.minutes, p.error_rate, fall=None, rng=rng)

        result.key_evidence = [
            warning.pid,
            *(e.pid for e in errors[:3]),
            *(ctx.metric_pid(db, MetricName.DISK_UTILIZATION, m) for m in minute_range(ctx, onset - 3, onset + 1)),
        ]
        result.root_cause = label(ctx, db)
        result.contributing_factors = ["the disk usage warning paged nobody"]
        return result
