"""A bad configuration change on the root Component shortly before onset (Req 23.1)."""

from datetime import timedelta
from typing import ClassVar

from pydantic import BaseModel, Field

from ...models.scenario import FaultTemplateName
from ...models.vocab import ChangeType, LogLevel, MetricName
from ..context import GenContext
from .base import FaultResult, TemplateParams, label, ramp, root_component


class ConfigRegressionParams(TemplateParams):
    setting: str = "db.pool.max_connections"
    old_value: str = "50"
    new_value: str = "5"
    change_lead_minutes: int = Field(12, ge=1, le=60)
    error_message: str = "request failed: timed out waiting for a database connection (pool size {new_value})"
    error_rate: float = Field(0.32, gt=0, le=1)
    error_logs: int = Field(24, ge=3)


class ConfigRegression:
    name: ClassVar[FaultTemplateName] = FaultTemplateName.CONFIG_REGRESSION
    params_model: ClassVar[type[BaseModel]] = ConfigRegressionParams

    def generate(self, ctx: GenContext, params: BaseModel) -> FaultResult:
        p = ConfigRegressionParams.model_validate(params.model_dump())
        root = root_component(ctx)
        result = FaultResult()
        rng = ctx.rng("fault")

        change = ctx.change(
            "fault",
            ctx.onset - timedelta(minutes=p.change_lead_minutes),
            root,
            ChangeType.CONFIG_CHANGE,
            f"Set {p.setting} from {p.old_value} to {p.new_value}",
            setting=p.setting,
            actor="config-pipeline",
        )
        result.changes.append(change)

        onset = ctx.onset_minute
        message = p.error_message.format(new_value=p.new_value)
        for i in range(p.error_logs):
            minute = onset + i * 1.5 + rng.uniform(0, 0.5)
            if minute >= ctx.minutes:
                break
            result.logs.append(ctx.log("fault", minute, root, LogLevel.ERROR, message))
        for caller in ctx.topology.downstream_of(root)[:1]:
            for i in range(5):
                minute = onset + 2 + i * 3
                if minute < ctx.minutes:
                    result.logs.append(ctx.log("fault", minute, caller, LogLevel.WARN, f"upstream {root} returned 503"))

        errors = ctx.get_series(root, MetricName.ERROR_RATE)
        ramp(errors, onset, onset + 3, p.error_rate)
        latency = ctx.get_series(root, MetricName.LATENCY_P99_MS)
        ramp(latency, onset, onset + 3, latency[onset] * 6)
        for caller in ctx.topology.downstream_of(root):
            if MetricName.ERROR_RATE in ctx.series_keys(caller):
                ramp(ctx.get_series(caller, MetricName.ERROR_RATE), onset + 1, onset + 4, p.error_rate / 3)

        result.key_evidence = [
            change.pid,
            *[log.pid for log in result.logs if log.level is LogLevel.ERROR][:3],
            *(ctx.metric_pid(root, MetricName.ERROR_RATE, m) for m in range(onset, min(onset + 5, ctx.minutes))),
        ]
        result.root_cause = label(ctx, root)
        result.contributing_factors = [f"no alert on {p.setting} changes"]
        return result
