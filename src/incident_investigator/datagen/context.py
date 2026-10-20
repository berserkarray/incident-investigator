"""Generation context: timing, topology, metric series and provisional record builders.

Records get provisional IDs (``log:fault:3``, ``met:<component>:<metric>:<minute>``, ...) so that
templates can name their key evidence; the generator maps them to opaque IDs at the end.
"""

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..models.scenario import ComponentRole, ScenarioSpec
from ..models.vocab import ChangeType, ComponentKind, LogLevel, MetricName
from .rng import stream
from .topology import Topology

# Baseline value and relative jitter of every background metric series, per component kind.
BASELINE_METRICS: dict[ComponentKind, dict[MetricName, tuple[float, float]]] = {
    ComponentKind.SERVICE: {
        MetricName.REQUEST_RATE: (120.0, 0.08),
        MetricName.ERROR_RATE: (0.004, 0.5),
        MetricName.LATENCY_P99_MS: (180.0, 0.1),
        MetricName.MEMORY_UTILIZATION: (0.45, 0.04),
    },
    ComponentKind.DATABASE: {
        MetricName.DB_CONNECTIONS: (60.0, 0.1),
        MetricName.LATENCY_P99_MS: (12.0, 0.15),
        MetricName.DISK_UTILIZATION: (0.55, 0.01),
    },
    ComponentKind.CACHE: {MetricName.CACHE_HIT_RATIO: (0.94, 0.01), MetricName.REQUEST_RATE: (900.0, 0.08)},
    ComponentKind.LOAD_BALANCER: {MetricName.REQUEST_RATE: (400.0, 0.06), MetricName.ERROR_RATE: (0.003, 0.5)},
    ComponentKind.THIRD_PARTY_API: {MetricName.LATENCY_P99_MS: (250.0, 0.12), MetricName.ERROR_RATE: (0.002, 0.5)},
    ComponentKind.QUEUE: {MetricName.REQUEST_RATE: (60.0, 0.1)},
}
# Baselines for series that only some templates create (for example CPU on one service).
ON_DEMAND_METRICS: dict[MetricName, tuple[float, float]] = {
    MetricName.CPU_UTILIZATION: (0.35, 0.08),
    MetricName.REPLICATION_LAG_S: (0.4, 0.3),
    MetricName.MEMORY_UTILIZATION: (0.45, 0.04),
    MetricName.DISK_UTILIZATION: (0.5, 0.01),
    MetricName.REQUEST_RATE: (100.0, 0.08),
    MetricName.ERROR_RATE: (0.004, 0.5),
    MetricName.LATENCY_P99_MS: (150.0, 0.1),
}


@dataclass
class ProvisionalLog:
    pid: str
    timestamp: datetime
    service: str
    level: LogLevel
    message: str
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class ProvisionalChange:
    pid: str
    timestamp: datetime
    service: str
    change_type: ChangeType
    summary: str
    attributes: dict[str, str] = field(default_factory=dict)


@dataclass
class ProvisionalPastIncident:
    pid: str
    occurred_at: datetime
    title: str
    summary: str
    affected_services: tuple[str, ...]
    symptoms: tuple[str, ...]
    cause_type: str
    resolution: str


@dataclass
class GenContext:
    spec: ScenarioSpec
    seed: int
    topology: Topology
    window_start: datetime
    window_end: datetime
    onset: datetime
    report_start: datetime
    series: dict[tuple[str, MetricName], list[float]] = field(default_factory=dict)
    _counters: dict[str, int] = field(default_factory=dict)

    @property
    def minutes(self) -> int:
        return int((self.window_end - self.window_start).total_seconds() // 60)

    @property
    def onset_minute(self) -> int:
        return int((self.onset - self.window_start).total_seconds() // 60)

    @property
    def lookback_start(self) -> datetime:
        return self.window_start - timedelta(days=self.spec.change_lookback_days)

    def rng(self, purpose: str) -> random.Random:
        return stream(self.seed, self.spec.name, purpose)

    def at_minute(self, minute: float) -> datetime:
        return self.window_start + timedelta(minutes=minute)

    @property
    def affected(self) -> str:
        return self.topology.role(ComponentRole.AFFECTED)

    def fill(self, text: str) -> str:
        """Replace ``{affected}`` in Incident_Report text with the affected service's name."""
        return text.replace("{affected}", self.affected)

    def report_title(self) -> str:
        return self.fill(self.spec.report.title)

    def report_symptoms(self) -> tuple[str, ...]:
        return tuple(self.fill(s) for s in self.spec.report.symptoms)

    def pid(self, prefix: str, scope: str) -> str:
        key = f"{prefix}:{scope}"
        n = self._counters.get(key, 0)
        self._counters[key] = n + 1
        return f"{key}:{n}"

    def metric_pid(self, component: str, metric: MetricName, minute: int) -> str:
        return f"met:{component}:{metric}:{minute}"

    def series_keys(self, component: str) -> set[MetricName]:
        return {metric for name, metric in self.series if name == component}

    def get_series(self, component: str, metric: MetricName) -> list[float]:
        """The minute-resolution series for a component metric, created at baseline if absent."""
        key = (component, metric)
        if key not in self.series:
            base, jitter = BASELINE_METRICS.get(self.topology.kind(component), {}).get(
                metric, ON_DEMAND_METRICS.get(metric, (1.0, 0.05))
            )
            rng = self.rng(f"series:{component}:{metric}")
            self.series[key] = [max(0.0, base * (1 + rng.uniform(-jitter, jitter))) for _ in range(self.minutes)]
        return self.series[key]

    def log(self, scope: str, minute: float, service: str, level: LogLevel, message: str, **attributes: str) -> ProvisionalLog:
        return ProvisionalLog(self.pid("log", scope), self.at_minute(minute), service, level, message, dict(attributes))

    def change(
        self, scope: str, timestamp: datetime, service: str, change_type: ChangeType, summary: str, **attributes: str
    ) -> ProvisionalChange:
        return ProvisionalChange(self.pid("chg", scope), timestamp, service, change_type, summary, dict(attributes))
