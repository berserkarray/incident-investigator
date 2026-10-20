"""Background telemetry, noise and past incidents (Req 22.9). None of it is ever Key_Evidence."""

from datetime import timedelta

from ..models.scenario import NoiseConstraints
from ..models.vocab import CauseType, ChangeType, ComponentKind, LogLevel
from .context import BASELINE_METRICS, GenContext, ProvisionalChange, ProvisionalLog, ProvisionalPastIncident

INFO_MESSAGES = (
    "GET /api/v1/items 200 in {ms} ms",
    "POST /api/v1/orders 201 in {ms} ms",
    "health check ok",
    "worker heartbeat",
)
NOISE_ERRORS = (
    "request aborted by client after {ms} ms",
    "retrying idempotent call after transient error",
    "malformed request body rejected",
    "slow consumer detected, backing off",
    "TLS handshake reset by peer",
)
NOISE_CHANGES = (
    (ChangeType.DEPLOYMENT, "Deploy build {n}: dependency bumps"),
    (ChangeType.CONFIG_CHANGE, "Rotate log retention to {n} days"),
    (ChangeType.FEATURE_FLAG_CHANGE, "Enable flag ui-refresh-{n} for internal users"),
    (ChangeType.DEPLOYMENT, "Deploy build {n}: copy changes"),
)
PAST_INCIDENT_TOPICS = (
    (CauseType.DNS_FAILURE, "Intermittent name resolution failures", "Resolver pool replaced"),
    (CauseType.RATE_LIMITING, "Partner API throttled requests", "Raised quota with the partner"),
    (CauseType.CODE_DEFECT, "Null handling bug in a request handler", "Hotfix deployed"),
    (CauseType.CAPACITY_LIMIT, "Worker pool saturated during a sale", "Scaled out the worker pool"),
    (CauseType.CLOCK_SKEW, "Token validation failed on skewed hosts", "NTP configuration fixed"),
)


def background_metrics(ctx: GenContext) -> None:
    for component in ctx.topology.components:
        for metric in BASELINE_METRICS.get(component.kind, {}):
            ctx.get_series(component.name, metric)


def background_logs(ctx: GenContext) -> list[ProvisionalLog]:
    rng = ctx.rng("background-logs")
    logs = []
    for component in ctx.topology.components:
        if component.kind not in (ComponentKind.SERVICE, ComponentKind.LOAD_BALANCER):
            continue
        minute = rng.uniform(0, 5)
        while minute < ctx.minutes:
            message = rng.choice(INFO_MESSAGES).format(ms=rng.randint(8, 240))
            logs.append(ctx.log("bg", minute, component.name, LogLevel.INFO, message))
            minute += rng.uniform(4, 6)
    return logs


def noise_logs(ctx: GenContext, clean_after_onset: set[str]) -> list[ProvisionalLog]:
    """Coincidental ERROR logs and noisy alerts scattered over the window.

    Components in ``clean_after_onset`` get no noise ERROR log at or after onset
    (``NoiseConstraints.no_error_logs_on_root_after_onset``).
    """
    rng = ctx.rng("noise-logs")
    services = [c.name for c in ctx.topology.components if c.kind in (ComponentKind.SERVICE, ComponentKind.LOAD_BALANCER)]
    logs: list[ProvisionalLog] = []
    while len([l for l in logs if l.level is LogLevel.ERROR]) < ctx.spec.noise.error_logs:
        service, minute = rng.choice(services), rng.uniform(0, ctx.minutes - 1)
        if service in clean_after_onset and minute >= ctx.onset_minute:
            continue
        logs.append(
            ctx.log("noise", minute, service, LogLevel.ERROR, rng.choice(NOISE_ERRORS).format(ms=rng.randint(100, 9000)))
        )
    for _ in range(ctx.spec.noise.noisy_alerts):
        service, minute = rng.choice(services), rng.uniform(0, ctx.minutes - 1)
        logs.append(
            ctx.log(
                "noise",
                minute,
                service,
                LogLevel.WARN,
                f"ALERT disk latency on {service} flapping (auto-resolved)",
                monitor="synthetic-probe",
                status="flapping",
            )
        )
    return logs


def noise_changes(ctx: GenContext, constraints: NoiseConstraints, no_deploy: set[str]) -> list[ProvisionalChange]:
    """Coincidental changes: at least one before the window.

    With ``no_changes_in_window`` every change lands before the window; Components in ``no_deploy``
    get configuration changes instead of deployments (``no_deploys_on_path_to_root``).
    """
    rng = ctx.rng("noise-changes")
    names = ctx.topology.names()
    lookback_minutes = (ctx.window_start - ctx.lookback_start).total_seconds() / 60
    changes: list[ProvisionalChange] = []
    for i in range(ctx.spec.noise.change_events):
        before_window = i == 0 or constraints.no_changes_in_window or rng.random() < 0.5
        if before_window:
            stamp = ctx.window_start - timedelta(minutes=rng.uniform(1, lookback_minutes - 1))
        else:
            stamp = ctx.at_minute(rng.uniform(0, ctx.minutes - 1))
        service = rng.choice(names)
        change_type, summary = rng.choice(NOISE_CHANGES)
        if change_type is ChangeType.DEPLOYMENT and service in no_deploy:
            change_type, summary = ChangeType.CONFIG_CHANGE, "Rotate log retention to {n} days"
        changes.append(ctx.change("noise", stamp, service, change_type, summary.format(n=rng.randint(10, 999))))
    return changes


def background_past_incidents(ctx: GenContext, avoid: set[str]) -> list[ProvisionalPastIncident]:
    rng = ctx.rng("past-incidents")
    candidates = [n for n in ctx.topology.names() if n not in avoid] or ctx.topology.names()
    incidents = []
    for i, (cause, title, resolution) in enumerate(rng.sample(PAST_INCIDENT_TOPICS, 2)):
        service = rng.choice(candidates)
        incidents.append(
            ProvisionalPastIncident(
                pid=ctx.pid("pin", "bg"),
                occurred_at=ctx.window_start - timedelta(days=rng.randint(20, 300), minutes=i),
                title=f"{title} on {service}",
                summary=f"{title}. Users saw elevated errors on {service}.",
                affected_services=(service,),
                symptoms=("elevated error rate", "increased latency"),
                cause_type=cause.value,
                resolution=resolution,
            )
        )
    return incidents
