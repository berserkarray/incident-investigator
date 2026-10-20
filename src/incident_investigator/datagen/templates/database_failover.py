"""The primary database loses its primary node and fails over to a lagging replica (Req 23.2)."""

from typing import ClassVar

from pydantic import BaseModel, Field

from ...models.scenario import FaultTemplateName
from ...models.vocab import LogLevel, MetricName
from ..context import GenContext
from .base import FaultResult, TemplateParams, label, minute_range, pulse, root_component, spaced_logs


class DatabaseFailoverParams(TemplateParams):
    lag_lead_minutes: int = Field(20, ge=5, le=60)
    lag_peak_s: float = Field(45.0, gt=1)
    failover_minutes: int = Field(6, ge=2, le=30)
    error_rate: float = Field(0.22, gt=0, le=1)


class DatabaseFailover:
    name: ClassVar[FaultTemplateName] = FaultTemplateName.DATABASE_FAILOVER
    params_model: ClassVar[type[BaseModel]] = DatabaseFailoverParams

    def generate(self, ctx: GenContext, params: BaseModel) -> FaultResult:
        p = DatabaseFailoverParams.model_validate(params.model_dump())
        db = root_component(ctx)
        affected = ctx.affected
        rng = ctx.rng("fault")
        onset = ctx.onset_minute
        recovered = onset + p.failover_minutes
        node_a, node_b = f"{db}-node-a", f"{db}-node-b"
        result = FaultResult()

        lag = ctx.get_series(db, MetricName.REPLICATION_LAG_S)
        pulse(lag, onset - p.lag_lead_minutes, recovered, p.lag_peak_s, rise=p.lag_lead_minutes, fall=5, rng=rng)
        pulse(ctx.get_series(db, MetricName.DB_CONNECTIONS), onset, recovered, lambda v: v * 0.08, fall=4, rng=rng)
        pulse(ctx.get_series(db, MetricName.LATENCY_P99_MS), onset, recovered, lambda v: v * 9, fall=4, rng=rng)
        pulse(ctx.get_series(affected, MetricName.ERROR_RATE), onset, recovered, p.error_rate, fall=3, rng=rng)

        lag_warning = ctx.log(
            "fault", onset - 3, db, LogLevel.WARN, f"replica {node_b} is {int(p.lag_peak_s * 0.7)} s behind primary {node_a}"
        )
        lost = ctx.log("fault", onset + 0.1, db, LogLevel.ERROR, f"primary {node_a} missed 3 heartbeats; marking it unreachable")
        started = ctx.log("fault", onset + 0.5, db, LogLevel.WARN, f"starting automatic failover of {db}: promoting {node_b}")
        promoted = ctx.log(
            "fault", recovered, db, LogLevel.WARN, f"{node_b} promoted to primary for {db}; writes accepted again"
        )
        result.logs += [lag_warning, lost, started, promoted]
        messages = (
            f"write to {db} failed: cannot execute UPDATE in a read-only transaction",
            f"connection to {db} reset during query (SQLSTATE 08006)",
        )
        errors = spaced_logs(
            ctx,
            rng,
            affected,
            LogLevel.ERROR,
            lambda i: messages[i % 2],
            start=onset + 0.2,
            count=p.failover_minutes * 2,
            every=0.5,
        )
        result.logs += errors
        result.logs.append(ctx.log("fault", recovered + 0.5, affected, LogLevel.INFO, f"reconnected to {db} primary {node_b}"))

        result.key_evidence = [
            lost.pid,
            started.pid,
            promoted.pid,
            *(e.pid for e in errors[:3]),
            *(ctx.metric_pid(db, MetricName.REPLICATION_LAG_S, m) for m in minute_range(ctx, onset - 5, onset)),
        ]
        result.root_cause = label(ctx, db)
        result.contributing_factors = [f"replica {node_b} lagged before the primary was lost"]
        return result
