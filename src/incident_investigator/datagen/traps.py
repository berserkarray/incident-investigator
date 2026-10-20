"""Trap generators: the red herrings a Scenario_Spec declares (Req 22.10, 23.2, 23.6-23.8).

Every red herring becomes snapshot records that look like ordinary telemetry and carry no marker;
their record IDs never enter the Key_Evidence. Misleading data is coincidental, never adversarial.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

from pydantic import BaseModel, Field, ValidationError

from ..models.scenario import ComponentRole, RedHerring, TrapKind
from ..models.vocab import CauseType, ChangeType, ComponentKind, LogLevel, MetricName
from .context import GenContext, ProvisionalChange, ProvisionalLog, ProvisionalPastIncident
from .templates.base import TemplateParams, pulse

LOGGING_KINDS = (ComponentKind.SERVICE, ComponentKind.LOAD_BALANCER)


class TrapError(ValueError):
    """A red herring cannot be generated as declared."""


@dataclass
class TrapOutput:
    logs: list[ProvisionalLog] = field(default_factory=list)
    changes: list[ProvisionalChange] = field(default_factory=list)
    past_incidents: list[ProvisionalPastIncident] = field(default_factory=list)
    metric_ids: list[str] = field(default_factory=list)  # provisional IDs of reshaped metric points

    def record_ids(self) -> list[str]:
        return [
            *(r.pid for r in self.logs),
            *(r.pid for r in self.changes),
            *(r.pid for r in self.past_incidents),
            *self.metric_ids,
        ]


@dataclass(frozen=True)
class TrapInput:
    ctx: GenContext
    herring: RedHerring
    index: int
    decoy: CauseType
    root: str | None

    def rng(self):
        return self.ctx.rng(f"trap:{self.index}:{self.herring.kind}")

    def target(self, default: ComponentRole, *, not_root: bool = False) -> str:
        role = self.herring.target_role or default
        names = self.ctx.topology.roles.get(role)
        if not names:
            raise TrapError(f"trap.red_herrings[{self.index}].target_role: no {role} component in this topology")
        if not_root and names[0] == self.root:
            raise TrapError(f"trap.red_herrings[{self.index}].target_role: {role} is the root Component")
        return names[0]

    @property
    def minute(self) -> float:
        return self.ctx.onset_minute + self.herring.offset_minutes

    def params(self, model: type[BaseModel]) -> BaseModel:
        try:
            return model.model_validate(self.herring.params)
        except ValidationError as exc:
            problems = "; ".join(
                f"trap.red_herrings[{self.index}].params.{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                for e in exc.errors(include_url=False)
            )
            raise TrapError(problems) from None


def _inside_window(trap: TrapInput, minute: float) -> float:
    if not 0 <= minute < trap.ctx.minutes:
        raise TrapError(f"trap.red_herrings[{trap.index}].offset_minutes: the red herring falls outside the window")
    return minute


# unrelated_deploy_near_onset


class UnrelatedDeployParams(TemplateParams):
    summary: str = Field("Deploy build {build}: update client library versions", min_length=1)
    instances: int = Field(12, ge=1)


def unrelated_deploy_near_onset(trap: TrapInput) -> TrapOutput:
    """A deployment on a non-root Component at exactly onset + offset (Req 23.2)."""
    p = trap.params(UnrelatedDeployParams)
    ctx, rng = trap.ctx, trap.rng()
    target = trap.target(ComponentRole.PEER, not_root=True)
    build = rng.randint(1000, 9999)
    stamp = ctx.onset + timedelta(minutes=trap.herring.offset_minutes)
    if not ctx.lookback_start <= stamp < ctx.window_end:
        raise TrapError(f"trap.red_herrings[{trap.index}].offset_minutes: the deployment falls outside the lookback")
    out = TrapOutput()
    out.changes.append(
        ctx.change("herring", stamp, target, ChangeType.DEPLOYMENT, p.summary.format(build=build), version=f"3.{build}.0")
    )
    if ctx.topology.kind(target) in LOGGING_KINDS and 0 <= trap.minute + 1.5 < ctx.minutes:
        out.logs.append(
            ctx.log(
                "herring",
                trap.minute + 1.5,
                target,
                LogLevel.INFO,
                f"rollout of build {build} finished: {p.instances}/{p.instances} instances healthy",
            )
        )
    return out


# noisy_alerts


class NoisyAlertsParams(TemplateParams):
    count: int = Field(6, ge=1, le=50)
    spread_minutes: float = Field(30, ge=0)
    monitor: str = Field("net-probe", min_length=1)


def noisy_alerts(trap: TrapInput) -> TrapOutput:
    """Firing network alerts scattered around onset on several Components."""
    p = trap.params(NoisyAlertsParams)
    ctx, rng = trap.ctx, trap.rng()
    services = [c.name for c in ctx.topology.components if c.kind in LOGGING_KINDS]
    out = TrapOutput()
    for i in range(p.count):
        minute = trap.minute + rng.uniform(-p.spread_minutes, p.spread_minutes)
        minute = min(max(0.0, minute), ctx.minutes - 0.01)
        out.logs.append(
            ctx.log(
                "herring",
                minute,
                rng.choice(services),
                LogLevel.WARN,
                f"ALERT packet loss {rng.uniform(1.5, 6.0):.1f}% between zone-a and zone-b (probe {rng.randint(1, 9)})",
                monitor=p.monitor,
                status="firing",
            )
        )
    return out


# innocent_config_change


class InnocentConfigParams(TemplateParams):
    setting: str = Field("log.sampling.rate", min_length=1)
    summary: str = Field("Raise request log sampling from 1% to 5%", min_length=1)


def innocent_config_change(trap: TrapInput) -> TrapOutput:
    """A harmless configuration change on a non-root Component near onset."""
    p = trap.params(InnocentConfigParams)
    ctx = trap.ctx
    target = trap.target(ComponentRole.AFFECTED, not_root=True)
    minute = _inside_window(trap, trap.minute)
    out = TrapOutput()
    out.changes.append(
        ctx.change("herring", ctx.at_minute(minute), target, ChangeType.CONFIG_CHANGE, p.summary, setting=p.setting)
    )
    if ctx.topology.kind(target) in LOGGING_KINDS and minute + 0.5 < ctx.minutes:
        out.logs.append(ctx.log("herring", minute + 0.5, target, LogLevel.INFO, f"configuration reloaded ({p.setting})"))
    return out


# traffic_spike


class TrafficSpikeParams(TemplateParams):
    factor: float = Field(6.0, ge=2)
    minutes: int = Field(4, ge=1)
    fall_minutes: int = Field(3, ge=0)
    client_logs: int = Field(4, ge=0)


def traffic_spike(trap: TrapInput) -> TrapOutput:
    """A burst of request traffic on the target and the entry load balancer that looks like an attack."""
    p = trap.params(TrafficSpikeParams)
    ctx, rng = trap.ctx, trap.rng()
    target = trap.target(ComponentRole.AFFECTED)
    start = int(_inside_window(trap, trap.minute))
    end = start + p.minutes
    out = TrapOutput()
    shaped = [target]
    edge = ctx.topology.roles.get(ComponentRole.ENTRY_LB, [None])[0]
    if edge is not None and edge != target:
        shaped.append(edge)
    for name in shaped:
        series = ctx.get_series(name, MetricName.REQUEST_RATE)
        pulse(series, start, end, lambda v: v * p.factor, fall=p.fall_minutes, rng=rng)
        out.metric_ids += [
            ctx.metric_pid(name, MetricName.REQUEST_RATE, m) for m in range(start, min(ctx.minutes, end + p.fall_minutes))
        ]
    talker = edge if edge is not None else target
    for i in range(p.client_logs):
        minute = start + i * (p.minutes / max(1, p.client_logs)) + 0.2
        if minute < ctx.minutes:
            out.logs.append(
                ctx.log(
                    "herring",
                    minute,
                    talker,
                    LogLevel.WARN,
                    f"burst from client 203.0.113.{rng.randint(2, 250)}: {rng.randint(1800, 4200)} requests/s (top talker)",
                )
            )
    return out


# disagreeing_monitors


class DisagreeingMonitorsParams(TemplateParams):
    minutes: int = Field(20, ge=10)
    healthy_monitor: str = Field("internal-probe", min_length=1)
    unhealthy_monitor: str = Field("edge-healthcheck", min_length=1)


def disagreeing_monitors(trap: TrapInput) -> TrapOutput:
    """Two monitors report opposite health for the same service, once a minute (Req 23.7)."""
    p = trap.params(DisagreeingMonitorsParams)
    if p.healthy_monitor == p.unhealthy_monitor:
        raise TrapError(f"trap.red_herrings[{trap.index}].params: the two monitors must differ")
    ctx, rng = trap.ctx, trap.rng()
    target = trap.target(ComponentRole.AFFECTED)
    start = _inside_window(trap, trap.minute)
    out = TrapOutput()
    for m in range(p.minutes + 1):
        healthy_at, unhealthy_at = start + m + 0.1, start + m + 0.3
        if unhealthy_at >= ctx.minutes:
            break
        out.logs.append(
            ctx.log(
                "herring",
                healthy_at,
                target,
                LogLevel.INFO,
                f"health check {target}: healthy (200 in {rng.randint(4, 30)} ms)",
                monitor=p.healthy_monitor,
                status="healthy",
            )
        )
        out.logs.append(
            ctx.log(
                "herring",
                unhealthy_at,
                target,
                LogLevel.WARN,
                f"health check {target}: unhealthy (3 consecutive failures)",
                monitor=p.unhealthy_monitor,
                status="unhealthy",
            )
        )
    return out


# lookalike_past_incident


class LookalikeParams(TemplateParams):
    days_before_window: int = Field(45, ge=1, le=3650)
    duration_minutes: int = Field(40, ge=1)


def lookalike_past_incident(trap: TrapInput) -> TrapOutput:
    """A past incident on the affected service with every reported symptom and a different cause (Req 23.8)."""
    p = trap.params(LookalikeParams)
    ctx = trap.ctx
    target = trap.target(ComponentRole.AFFECTED)
    cause_words = trap.decoy.value.replace("_", " ")
    out = TrapOutput()
    out.past_incidents.append(
        ProvisionalPastIncident(
            pid=ctx.pid("pin", "herring"),
            occurred_at=ctx.window_start - timedelta(days=p.days_before_window),
            title=ctx.report_title(),
            summary=f"Error rate and latency rose on {target} for {p.duration_minutes} minutes until its pods were restarted.",
            affected_services=(target,),
            symptoms=ctx.report_symptoms(),
            cause_type=trap.decoy.value,
            resolution=f"Restarted {target}; the follow-up fixed a {cause_words} in {target}.",
        )
    )
    return out


TrapFn = Callable[[TrapInput], TrapOutput]
TRAPS: dict[TrapKind, TrapFn] = {
    TrapKind.UNRELATED_DEPLOY_NEAR_ONSET: unrelated_deploy_near_onset,
    TrapKind.NOISY_ALERTS: noisy_alerts,
    TrapKind.INNOCENT_CONFIG_CHANGE: innocent_config_change,
    TrapKind.TRAFFIC_SPIKE: traffic_spike,
    TrapKind.DISAGREEING_MONITORS: disagreeing_monitors,
    TrapKind.LOOKALIKE_PAST_INCIDENT: lookalike_past_incident,
}


def generate_red_herrings(ctx: GenContext, root: str | None) -> list[TrapOutput]:
    """One output per declared red herring, in declaration order."""
    trap = ctx.spec.trap
    if trap is None:
        return []
    return [TRAPS[h.kind](TrapInput(ctx, h, i, trap.decoy_cause_type, root)) for i, h in enumerate(trap.red_herrings)]
