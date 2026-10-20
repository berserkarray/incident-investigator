"""Cache keys sharing one TTL expire together; misses pile onto the database (Req 23.6)."""

from typing import ClassVar

from pydantic import BaseModel, Field

from ...models.scenario import ComponentRole, FaultTemplateName
from ...models.vocab import LogLevel, MetricName
from ..context import GenContext
from .base import FaultResult, TemplateParams, label, minute_range, pulse, root_component, spaced_logs


class CacheStampedeParams(TemplateParams):
    expiry_lead_minutes: int = Field(1, ge=1, le=5)  # the hit ratio drops this many minutes before onset
    hit_ratio_floor: float = Field(0.12, ge=0, lt=0.8)
    recovery_minutes: int = Field(25, ge=5)
    db_connection_factor: float = Field(7.0, gt=1)
    error_rate: float = Field(0.15, gt=0, le=1)


class CacheStampede:
    name: ClassVar[FaultTemplateName] = FaultTemplateName.CACHE_STAMPEDE
    params_model: ClassVar[type[BaseModel]] = CacheStampedeParams

    def generate(self, ctx: GenContext, params: BaseModel) -> FaultResult:
        p = CacheStampedeParams.model_validate(params.model_dump())
        cache = root_component(ctx)
        db = ctx.topology.role(ComponentRole.PRIMARY_DB)
        affected = ctx.affected
        rng = ctx.rng("fault")
        onset = ctx.onset_minute
        drop = onset - p.expiry_lead_minutes
        result = FaultResult()

        hit_ratio = ctx.get_series(cache, MetricName.CACHE_HIT_RATIO)
        pulse(hit_ratio, drop, drop + 3, p.hit_ratio_floor, fall=p.recovery_minutes, rng=rng)
        pulse(ctx.get_series(cache, MetricName.REQUEST_RATE), drop, drop + 10, lambda v: v * 3, fall=8, rng=rng)
        pulse(ctx.get_series(db, MetricName.DB_CONNECTIONS), onset, onset + 8, lambda v: v * p.db_connection_factor, fall=6, rng=rng)
        pulse(ctx.get_series(db, MetricName.LATENCY_P99_MS), onset, onset + 8, lambda v: v * 6, fall=6, rng=rng)
        pulse(ctx.get_series(affected, MetricName.ERROR_RATE), onset, onset + 10, p.error_rate, fall=10, rng=rng)
        pulse(ctx.get_series(affected, MetricName.LATENCY_P99_MS), onset, onset + 10, lambda v: v * 5, fall=10, rng=rng)

        expired = ctx.log(
            "fault",
            drop + 0.1,
            cache,
            LogLevel.WARN,
            f"{rng.randint(900_000, 1_400_000)} keys expired within one second (shared TTL 3600 s on catalog:*)",
        )
        result.logs.append(expired)
        misses = spaced_logs(
            ctx,
            rng,
            affected,
            LogLevel.WARN,
            lambda i: f"cache miss on catalog:item:{rng.randint(1000, 9999)}; rebuilding from {db} ({rng.randint(200, 450)} concurrent rebuilds)",
            start=drop + 0.2,
            count=12,
            every=1.0,
        )
        result.logs += misses
        result.logs += spaced_logs(
            ctx,
            rng,
            affected,
            LogLevel.ERROR,
            f"timed out after 3000 ms waiting for {db} during cache rebuild",
            start=onset + 1,
            count=15,
            every=1.1,
        )

        result.key_evidence = [
            expired.pid,
            misses[0].pid,
            *(ctx.metric_pid(cache, MetricName.CACHE_HIT_RATIO, m) for m in minute_range(ctx, drop, drop + 2)),
            *(ctx.metric_pid(db, MetricName.DB_CONNECTIONS, m) for m in minute_range(ctx, onset, onset + 3)),
        ]
        result.root_cause = label(ctx, cache)
        result.contributing_factors = ["one TTL for every catalog key", "no request coalescing on cache misses"]
        return result
