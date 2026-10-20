"""A deployment days before the window starts leaking memory; the service is killed and restarts (Req 23.4)."""

from datetime import timedelta
from typing import ClassVar

from pydantic import BaseModel, Field

from ...models.scenario import FaultTemplateName
from ...models.vocab import ChangeType, LogLevel, MetricName
from ..context import GenContext
from .base import FaultResult, TemplateParams, label, minute_range, pulse, root_component, spaced_logs


class MemoryLeakParams(TemplateParams):
    change_days_before_window: float = Field(3.0, ge=2.0, le=29.0)
    peak_utilization: float = Field(0.96, gt=0.6, le=1.0)
    restart_cycle_minutes: int = Field(25, ge=10)
    oom_kills: int = Field(3, ge=1)


class MemoryLeak:
    name: ClassVar[FaultTemplateName] = FaultTemplateName.MEMORY_LEAK
    params_model: ClassVar[type[BaseModel]] = MemoryLeakParams

    def generate(self, ctx: GenContext, params: BaseModel) -> FaultResult:
        p = MemoryLeakParams.model_validate(params.model_dump())
        root = root_component(ctx)
        callers = ctx.topology.downstream_of(root)
        rng = ctx.rng("fault")
        onset = ctx.onset_minute
        result = FaultResult()

        build = rng.randint(100, 999)
        stamp = ctx.window_start - timedelta(days=p.change_days_before_window, minutes=rng.randint(0, 600))
        if stamp < ctx.lookback_start:
            raise ValueError("fault.params.change_days_before_window: the change falls before the change lookback")
        deploy = ctx.change(
            "fault",
            stamp,
            root,
            ChangeType.DEPLOYMENT,
            f"Deploy build {build}: add in-process response cache for /api/v1/catalog",
            version=f"2.{build}.0",
        )
        result.changes.append(deploy)

        # Memory climbs from the window start to the first kill, then saw-tooths after each restart.
        memory = ctx.get_series(root, MetricName.MEMORY_UTILIZATION)
        base = memory[0]
        for i in range(min(onset, ctx.minutes)):
            memory[i] = base + (p.peak_utilization - base) * (i / max(1, onset)) + rng.uniform(-0.004, 0.004)
        floor = 0.3
        for i in range(onset, ctx.minutes):
            phase = (i - onset) % p.restart_cycle_minutes
            memory[i] = floor + (p.peak_utilization - floor) * (phase / p.restart_cycle_minutes) + rng.uniform(-0.004, 0.004)
        pulse(ctx.get_series(root, MetricName.LATENCY_P99_MS), onset - 40, onset, lambda v: v * 3, rise=40, fall=2, rng=rng)

        gc_logs = [
            ctx.log("fault", onset - lead, root, LogLevel.WARN, f"GC pause of {pause} ms (heap {heap}% used)")
            for lead, pause, heap in ((30, 420, 88), (20, 910, 92), (10, 1650, 95))
            if onset - lead >= 0
        ]
        result.logs += gc_logs
        kills = []
        for k in range(p.oom_kills):
            at = onset + k * p.restart_cycle_minutes
            if at >= ctx.minutes:
                break
            kill = ctx.log("fault", at, root, LogLevel.ERROR, f"{root} worker killed by OOM killer (rss=3.9 GiB, limit=4 GiB)")
            kills.append(kill)
            result.logs.append(kill)
            if at + 1.2 < ctx.minutes:
                result.logs.append(ctx.log("fault", at + 1.2, root, LogLevel.INFO, f"{root} restarted (restart #{k + 1})"))
            for caller in callers:
                result.logs += spaced_logs(
                    ctx, rng, caller, LogLevel.ERROR, f"upstream {root} connection refused", start=at + 0.1, count=4, every=0.6
                )
                if MetricName.ERROR_RATE in ctx.series_keys(caller):
                    pulse(ctx.get_series(caller, MetricName.ERROR_RATE), at, at + 3, 0.2, fall=2, rng=rng)

        result.key_evidence = [
            deploy.pid,
            *(k.pid for k in kills[:2]),
            *(g.pid for g in gc_logs[-1:]),
            *(ctx.metric_pid(root, MetricName.MEMORY_UTILIZATION, m) for m in minute_range(ctx, onset - 3, onset)),
        ]
        result.root_cause = label(ctx, root)
        result.contributing_factors = ["no memory-growth alert on the service"]
        return result
