"""A feature flag enabled for one cohort breaks requests of that cohort only (Req 23.11)."""

from datetime import timedelta
from typing import ClassVar

from pydantic import BaseModel, Field

from ...models.scenario import FaultTemplateName, NoiseConstraints
from ...models.vocab import ChangeType, LogLevel, MetricName
from ..context import GenContext
from .base import FaultResult, TemplateParams, label, pulse, root_component, spaced_logs


class FeatureFlagRegressionParams(TemplateParams):
    flag: str = Field("pricing-engine-v2", min_length=1)
    cohort: str = Field("beta-eu", min_length=1)
    control_cohort: str = Field("control", min_length=1)
    rollout_percent: int = Field(20, ge=1, le=100)
    lead_minutes: int = Field(10, ge=1, le=60)
    error_logs: int = Field(30, ge=3)
    error_rate: float = Field(0.08, gt=0, le=1)


class FeatureFlagRegression:
    name: ClassVar[FaultTemplateName] = FaultTemplateName.FEATURE_FLAG_REGRESSION
    params_model: ClassVar[type[BaseModel]] = FeatureFlagRegressionParams

    def generate(self, ctx: GenContext, params: BaseModel) -> FaultResult:
        p = FeatureFlagRegressionParams.model_validate(params.model_dump())
        if p.cohort == p.control_cohort:
            raise ValueError("fault.params.control_cohort: must differ from cohort")
        root = root_component(ctx)
        rng = ctx.rng("fault")
        onset = ctx.onset_minute
        result = FaultResult(noise_constraints=NoiseConstraints(no_error_logs_on_root_after_onset=True))

        change = ctx.change(
            "fault",
            ctx.onset - timedelta(minutes=p.lead_minutes),
            root,
            ChangeType.FEATURE_FLAG_CHANGE,
            f"Enable flag {p.flag} for cohort {p.cohort} ({p.rollout_percent}%)",
            flag=p.flag,
            cohort=p.cohort,
        )
        result.changes.append(change)
        errors = spaced_logs(
            ctx,
            rng,
            root,
            LogLevel.ERROR,
            f"price calculation failed: {p.flag} returned no total for cart",
            start=onset + 0.05,
            count=p.error_logs,
            every=1.2,
            cohort=p.cohort,
        )
        result.logs += errors
        result.logs += spaced_logs(
            ctx,
            rng,
            root,
            LogLevel.INFO,
            lambda i: f"checkout completed in {rng.randint(80, 240)} ms",
            start=onset + 0.3,
            count=10,
            every=2.5,
            cohort=p.control_cohort,
        )
        pulse(ctx.get_series(root, MetricName.ERROR_RATE), onset, ctx.minutes, p.error_rate, fall=None, rng=rng)

        result.key_evidence = [change.pid, *(e.pid for e in errors[:3])]
        result.root_cause = label(ctx, root)
        result.contributing_factors = [f"{p.flag} skipped the canary stage"]
        return result
