"""Incident_Snapshot: the fixed data set that the Tools read (Req 2, 14.9)."""

from typing import Literal

from pydantic import Field, model_validator

from .base import AwareDatetime, StrictModel, TimeRange
from .vocab import (
    NO_STALE_TOOLS,
    CauseType,
    ChangeType,
    ComponentKind,
    FailureType,
    LogLevel,
    MetricName,
    ToolName,
)


class Component(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    kind: ComponentKind


class DependencyEdge(StrictModel):
    """``dependent`` calls ``dependency``; the dependency is upstream of the dependent."""

    record_id: str
    dependent: str
    dependency: str


class LogRecord(StrictModel):
    kind: Literal["log"] = "log"
    record_id: str
    timestamp: AwareDatetime
    service: str
    level: LogLevel
    message: str
    attributes: dict[str, str] = {}


class MetricPoint(StrictModel):
    """One native-resolution (60 s) metric sample. Tools return aggregated buckets instead."""

    record_id: str
    timestamp: AwareDatetime
    service: str
    metric_name: MetricName
    value: float


class ChangeEvent(StrictModel):
    kind: Literal["change"] = "change"
    record_id: str
    timestamp: AwareDatetime
    service: str
    change_type: ChangeType
    summary: str
    attributes: dict[str, str] = {}


class PastIncident(StrictModel):
    kind: Literal["past_incident"] = "past_incident"
    record_id: str
    occurred_at: AwareDatetime
    title: str
    summary: str
    affected_services: tuple[str, ...]
    symptoms: tuple[str, ...]
    cause_type: CauseType
    resolution: str


class ForcedFailure(StrictModel):
    tool: ToolName
    failure_type: FailureType


class FailureProfile(StrictModel):
    """Forced Tool failures for one snapshot. Contains no root-cause information."""

    forced: tuple[ForcedFailure, ...] = ()

    @model_validator(mode="after")
    def _valid_entries(self) -> "FailureProfile":
        seen: set[ToolName] = set()
        problems: list[str] = []
        for index, entry in enumerate(self.forced):
            if entry.tool in seen:
                problems.append(f"forced[{index}]: more than one forced failure for {entry.tool}")
            seen.add(entry.tool)
            if entry.failure_type is FailureType.STALE and entry.tool in NO_STALE_TOOLS:
                problems.append(f"forced[{index}]: stale_data is not allowed for {entry.tool}")
        if problems:
            raise ValueError("; ".join(problems))
        return self

    def forced_for(self, tool: ToolName) -> FailureType | None:
        for entry in self.forced:
            if entry.tool is tool:
                return entry.failure_type
        return None


class IncidentSnapshot(StrictModel):
    """``snapshot.json`` plus ``failure_profile.json`` of one incident."""

    schema_version: Literal[1] = 1
    incident_id: str
    telemetry_window: TimeRange
    components: tuple[Component, ...] = Field(min_length=1)
    dependencies: tuple[DependencyEdge, ...] = ()
    logs: tuple[LogRecord, ...] = ()
    metrics: tuple[MetricPoint, ...] = ()
    changes: tuple[ChangeEvent, ...] = ()
    past_incidents: tuple[PastIncident, ...] = ()
    failure_profile: FailureProfile = FailureProfile()

    @model_validator(mode="after")
    def _unique_components(self) -> "IncidentSnapshot":
        names = [c.name for c in self.components]
        if len(names) != len(set(names)):
            raise ValueError("component names must be unique")
        return self

    @property
    def component_names(self) -> frozenset[str]:
        return frozenset(c.name for c in self.components)

    def component_kind(self, name: str) -> ComponentKind | None:
        for component in self.components:
            if component.name == name:
                return component.kind
        return None
