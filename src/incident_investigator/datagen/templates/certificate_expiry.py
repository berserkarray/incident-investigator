"""The serving certificate of an upstream service expires at onset; its callers fail TLS handshakes (Req 23.5)."""

from typing import ClassVar

from pydantic import BaseModel, Field

from ...models.scenario import FaultTemplateName, NoiseConstraints
from ...models.vocab import LogLevel, MetricName
from ..context import GenContext
from .base import FaultResult, TemplateParams, label, minute_range, pulse, root_component, spaced_logs


class CertificateExpiryParams(TemplateParams):
    warning_lead_minutes: int = Field(120, ge=5)
    error_logs: int = Field(30, ge=3)
    error_rate: float = Field(0.45, gt=0, le=1)


class CertificateExpiry:
    name: ClassVar[FaultTemplateName] = FaultTemplateName.CERTIFICATE_EXPIRY
    params_model: ClassVar[type[BaseModel]] = CertificateExpiryParams

    def generate(self, ctx: GenContext, params: BaseModel) -> FaultResult:
        p = CertificateExpiryParams.model_validate(params.model_dump())
        root = root_component(ctx)
        callers = ctx.topology.downstream_of(root)
        caller = ctx.affected if ctx.affected in callers else (callers[0] if callers else ctx.affected)
        rng = ctx.rng("fault")
        onset = ctx.onset_minute
        common_name = f"{root}.svc.internal"
        expiry = ctx.onset.strftime("%Y-%m-%dT%H:%M:%SZ")
        result = FaultResult(noise_constraints=NoiseConstraints(no_deploys_on_path_to_root=True))

        warning = ctx.log(
            "fault",
            max(0.5, onset - p.warning_lead_minutes),
            root,
            LogLevel.WARN,
            f"serving certificate CN={common_name} expires at {expiry} (notAfter)",
        )
        result.logs.append(warning)
        errors = spaced_logs(
            ctx,
            rng,
            caller,
            LogLevel.ERROR,
            f"TLS handshake with {root} failed: x509: certificate has expired (CN={common_name})",
            start=onset + 0.05,
            count=p.error_logs,
            every=1.2,
        )
        result.logs += errors
        result.logs += spaced_logs(
            ctx,
            rng,
            root,
            LogLevel.WARN,
            f"TLS handshake error from {caller}: remote error: bad certificate",
            start=onset + 0.5,
            count=5,
            every=4,
        )

        pulse(ctx.get_series(root, MetricName.REQUEST_RATE), onset, ctx.minutes, lambda v: v * 0.04, fall=None, rng=rng)
        pulse(ctx.get_series(caller, MetricName.ERROR_RATE), onset, ctx.minutes, p.error_rate, fall=None, rng=rng)

        result.key_evidence = [
            warning.pid,
            *(e.pid for e in errors[:3]),
            *(ctx.metric_pid(root, MetricName.REQUEST_RATE, m) for m in minute_range(ctx, onset, onset + 3)),
        ]
        result.root_cause = label(ctx, root)
        result.contributing_factors = ["certificate renewal is manual and was not tracked"]
        return result
