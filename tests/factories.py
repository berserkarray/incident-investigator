"""Small builders for test data. Keep them tiny: tests state only what they care about."""

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from incident_investigator.models.base import TimeRange
from incident_investigator.models.config import Config, parse_config
from incident_investigator.models.evidence import EvidenceItem, ObservationSummary
from incident_investigator.models.incident import IncidentReport
from incident_investigator.models.snapshot import (
    ChangeEvent,
    Component,
    DependencyEdge,
    IncidentSnapshot,
    LogRecord,
    MetricPoint,
)
from incident_investigator.models.tools import QueryLogsInput
from incident_investigator.models.vocab import (
    AgentName,
    ChangeType,
    ComponentKind,
    LogLevel,
    MetricName,
    Reliability,
    ToolName,
)

T0 = datetime(2024, 1, 1, tzinfo=UTC)


def at(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def window(start_min: float = 0, end_min: float = 120) -> TimeRange:
    return TimeRange(start=at(start_min), end=at(end_min))


def make_incident_report(**overrides: Any) -> IncidentReport:
    data: dict[str, Any] = {
        "title": "Checkout errors",
        "symptoms": ["HTTP 500 rate above 5% on checkout"],
        "affected_service": "checkout-a1b",
        "start_time": at(60).isoformat(),
        "severity": "sev2",
    }
    data.update(overrides)
    return IncidentReport.model_validate(data)


SERVICES = (
    ("lb-0aa", ComponentKind.LOAD_BALANCER),
    ("checkout-a1b", ComponentKind.SERVICE),
    ("payments-c2d", ComponentKind.SERVICE),
    ("inventory-e3f", ComponentKind.SERVICE),
    ("search-123", ComponentKind.SERVICE),
    ("orders-db-456", ComponentKind.DATABASE),
    ("session-cache-789", ComponentKind.CACHE),
    ("card-api-abc", ComponentKind.THIRD_PARTY_API),
)


def make_snapshot(**overrides: Any) -> IncidentSnapshot:
    """An 8-component, 2-hour snapshot with a handful of records."""
    components = tuple(Component(name=n, kind=k) for n, k in SERVICES)
    edges = (
        DependencyEdge(record_id="dep-1", dependent="lb-0aa", dependency="checkout-a1b"),
        DependencyEdge(record_id="dep-2", dependent="checkout-a1b", dependency="payments-c2d"),
        DependencyEdge(record_id="dep-3", dependent="checkout-a1b", dependency="orders-db-456"),
        DependencyEdge(record_id="dep-4", dependent="payments-c2d", dependency="card-api-abc"),
    )
    logs = (
        LogRecord(record_id="log-1", timestamp=at(10), service="checkout-a1b", level=LogLevel.INFO, message="ok"),
        LogRecord(
            record_id="log-2", timestamp=at(61), service="checkout-a1b", level=LogLevel.ERROR, message="timeout 504"
        ),
    )
    metrics = (
        MetricPoint(
            record_id="met-1", timestamp=at(60), service="checkout-a1b", metric_name=MetricName.ERROR_RATE, value=0.2
        ),
    )
    changes = (
        ChangeEvent(
            record_id="chg-1",
            timestamp=at(50),
            service="checkout-a1b",
            change_type=ChangeType.CONFIG_CHANGE,
            summary="pool size 50 -> 5",
        ),
    )
    data: dict[str, Any] = {
        "incident_id": "inc-test0001",
        "telemetry_window": window(0, 120),
        "components": components,
        "dependencies": edges,
        "logs": logs,
        "metrics": metrics,
        "changes": changes,
    }
    data.update(overrides)
    return IncidentSnapshot(**data)


def make_config(**sections: Any) -> Config:
    """A fake-provider Config; ``sections`` are merged as top-level TOML-like mappings."""
    data: dict[str, Any] = {"llm": {"provider": "fake", "model": "offline-responder"}}
    for key, value in sections.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key] = {**data[key], **value}
        else:
            data[key] = value
    return parse_config(data, source=Path("<test>"))


def make_evidence_item(
    evidence_id: str = "E0001",
    *,
    seq: int = 1,
    tool: ToolName = ToolName.QUERY_LOGS,
    reliability: Reliability = Reliability.COMPLETE,
    source_record_ids: tuple[str, ...] = ("log-2",),
) -> EvidenceItem:
    query = QueryLogsInput(service="checkout-a1b", time_range=window(0, 120), limit=25)
    return EvidenceItem(
        evidence_id=evidence_id,
        seq=seq,
        tool=tool,
        query=query,
        observation=ObservationSummary(record_count=len(source_record_ids)),
        time_range=query.time_range,
        reliability=reliability,
        source_record_ids=source_record_ids,
        produced_by=AgentName.OBSERVABILITY,
        task_id="T1",
        recorded_at=at(0),
    )


def write_snapshot_dir(root: Path, snapshot: IncidentSnapshot | None = None, report: IncidentReport | None = None) -> Path:
    """Write ``snapshot.json``, ``failure_profile.json`` and ``incident_report.json`` under ``root/<incident_id>``."""
    snapshot = snapshot or make_snapshot()
    directory = Path(root) / snapshot.incident_id
    directory.mkdir(parents=True)
    (directory / "snapshot.json").write_text(snapshot.model_dump_json(exclude={"failure_profile"}))
    (directory / "failure_profile.json").write_text(snapshot.failure_profile.model_dump_json())
    (directory / "incident_report.json").write_text((report or make_incident_report()).model_dump_json())
    return directory
