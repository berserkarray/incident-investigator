"""The primary database reaches its connection limit; callers time out acquiring connections.

The Key_Evidence is metrics and dependency edges only, so the scenario stays solvable when
query_logs is unavailable (Req 23.9).
"""

from typing import ClassVar

from pydantic import BaseModel, Field

from ...models.scenario import FaultTemplateName
from ...models.vocab import LogLevel, MetricName
from ..context import GenContext
from .base import FaultResult, TemplateParams, label, minute_range, pulse, root_component, spaced_logs


class ConnectionPoolExhaustionParams(TemplateParams):
    max_connections: int = Field(500, ge=20)
    ramp_minutes: int = Field(15, ge=1)
    latency_factor: float = Field(10.0, gt=1)
    error_rate: float = Field(0.35, gt=0, le=1)


class ConnectionPoolExhaustion:
    name: ClassVar[FaultTemplateName] = FaultTemplateName.CONNECTION_POOL_EXHAUSTION
    params_model: ClassVar[type[BaseModel]] = ConnectionPoolExhaustionParams

    def generate(self, ctx: GenContext, params: BaseModel) -> FaultResult:
        p = ConnectionPoolExhaustionParams.model_validate(params.model_dump())
        db = root_component(ctx)
        affected = ctx.affected
        if db not in ctx.topology.upstream_of(affected):
            raise ValueError(f"{affected} does not depend on {db} directly")
        rng = ctx.rng("fault")
        onset = ctx.onset_minute
        result = FaultResult()

        connections = ctx.get_series(db, MetricName.DB_CONNECTIONS)
        start = max(0, onset - p.ramp_minutes)
        base = connections[start]
        for i in range(start, min(onset, ctx.minutes)):
            connections[i] = base + (p.max_connections - base) * (i - start) / max(1, onset - start)
        for i in range(onset, ctx.minutes):
            connections[i] = float(p.max_connections - rng.choice((0, 0, 0, 1, 2)))
        pulse(ctx.get_series(db, MetricName.LATENCY_P99_MS), onset, ctx.minutes, lambda v: v * 6, fall=None, rng=rng)
        latency = ctx.get_series(affected, MetricName.LATENCY_P99_MS)
        pulse(latency, onset, ctx.minutes, lambda v: v * p.latency_factor, fall=None, rng=rng)
        pulse(ctx.get_series(affected, MetricName.ERROR_RATE), onset, ctx.minutes, p.error_rate, fall=None, rng=rng)

        result.logs += spaced_logs(
            ctx,
            rng,
            db,
            LogLevel.ERROR,
            f"FATAL: sorry, too many clients already (max_connections={p.max_connections})",
            start=onset,
            count=20,
            every=1.5,
        )
        result.logs += spaced_logs(
            ctx,
            rng,
            affected,
            LogLevel.ERROR,
            f"timed out acquiring a connection to {db} after 3000 ms",
            start=onset + 0.2,
            count=30,
            every=1.0,
        )

        result.key_evidence = [
            f"dep:{affected}>{db}",
            *(ctx.metric_pid(db, MetricName.DB_CONNECTIONS, m) for m in minute_range(ctx, onset - 2, onset + 3)),
            *(ctx.metric_pid(affected, MetricName.LATENCY_P99_MS, m) for m in minute_range(ctx, onset, onset + 3)),
        ]
        result.root_cause = label(ctx, db)
        result.contributing_factors = ["connection limits are not sized for peak concurrency"]
        return result
