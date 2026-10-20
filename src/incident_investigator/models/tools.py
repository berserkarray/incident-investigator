"""Typed Tool inputs, Tool records and Tool_Results (Req 13)."""

from collections.abc import Mapping
from datetime import datetime
from typing import Annotated, Literal

from pydantic import Field, StrictInt, TypeAdapter, field_validator, model_validator

from .base import AwareDatetime, StrictModel, TimeRange
from .snapshot import ChangeEvent, LogRecord, PastIncident
from .vocab import ChangeType, ComponentKind, ErrorCode, LogLevel, MetricName, ToolName

DEFAULT_RECORD_LIMIT = 100
MAX_RECORD_LIMIT = 500


class ToolInputBase(StrictModel):
    limit: int = Field(DEFAULT_RECORD_LIMIT, ge=1, le=MAX_RECORD_LIMIT)


def _normalise_enum_tuple(value, field_name: str):
    """Sort and de-duplicate an optional tuple of enum values; an empty tuple is invalid."""
    if value is None:
        return None
    if len(value) == 0:
        raise ValueError(f"{field_name} must not be empty when given")
    return tuple(sorted(set(value), key=lambda item: item.value))


class QueryLogsInput(ToolInputBase):
    tool: Literal["query_logs"] = "query_logs"
    service: str = Field(min_length=1, max_length=100)
    time_range: TimeRange
    levels: tuple[LogLevel, ...] | None = None
    text: str | None = Field(None, min_length=1, max_length=200)
    attributes: dict[str, str] | None = Field(None, max_length=10)

    @field_validator("levels")
    @classmethod
    def _norm_levels(cls, value):
        return _normalise_enum_tuple(value, "levels")


class QueryMetricsInput(ToolInputBase):
    tool: Literal["query_metrics"] = "query_metrics"
    service: str = Field(min_length=1, max_length=100)
    metric_name: MetricName
    time_range: TimeRange
    step_seconds: StrictInt = Field(300, ge=60, le=3600)


class GetDeploymentsInput(ToolInputBase):
    tool: Literal["get_deployments"] = "get_deployments"
    time_range: TimeRange
    service: str | None = Field(None, min_length=1, max_length=100)
    change_types: tuple[ChangeType, ...] | None = None

    @field_validator("change_types")
    @classmethod
    def _norm_change_types(cls, value):
        return _normalise_enum_tuple(value, "change_types")


class GetDependenciesInput(ToolInputBase):
    tool: Literal["get_dependencies"] = "get_dependencies"
    service: str = Field(min_length=1, max_length=100)
    depth: int = Field(1, ge=1, le=5)


class SearchIncidentsInput(ToolInputBase):
    tool: Literal["search_incidents"] = "search_incidents"
    query: str = Field(min_length=1, max_length=200)
    service: str | None = Field(None, min_length=1, max_length=100)


ToolInput = Annotated[
    QueryLogsInput | QueryMetricsInput | GetDeploymentsInput | GetDependenciesInput | SearchIncidentsInput,
    Field(discriminator="tool"),
]
TOOL_INPUT_ADAPTER: TypeAdapter = TypeAdapter(ToolInput)

TOOL_INPUT_MODELS: Mapping[ToolName, type[ToolInputBase]] = {
    ToolName.QUERY_LOGS: QueryLogsInput,
    ToolName.QUERY_METRICS: QueryMetricsInput,
    ToolName.GET_DEPLOYMENTS: GetDeploymentsInput,
    ToolName.GET_DEPENDENCIES: GetDependenciesInput,
    ToolName.SEARCH_INCIDENTS: SearchIncidentsInput,
}


def input_time_range(tool_input: ToolInputBase) -> TimeRange | None:
    """The time range of a Tool input, or None for Tools whose input has none."""
    return getattr(tool_input, "time_range", None)


def input_service(tool_input: ToolInputBase) -> str | None:
    return getattr(tool_input, "service", None)


class MetricBucket(StrictModel):
    """One aggregation step of a metric series. ``constituent_ids`` are the raw MetricPoint IDs."""

    kind: Literal["metric_bucket"] = "metric_bucket"
    record_id: str
    timestamp: AwareDatetime
    service: str
    metric_name: MetricName
    step_seconds: int
    mean: float
    min: float
    max: float
    count: int = Field(ge=1)
    constituent_ids: tuple[str, ...]


class DependencyRecord(StrictModel):
    """One Component reachable from the requested service. ``record_id`` is the final-hop edge ID."""

    kind: Literal["dependency"] = "dependency"
    record_id: str
    component: str
    component_kind: ComponentKind
    direction: Literal["upstream", "downstream"]
    hops: int = Field(ge=1)
    constituent_ids: tuple[str, ...]


ToolRecord = Annotated[
    LogRecord | MetricBucket | ChangeEvent | DependencyRecord | PastIncident,
    Field(discriminator="kind"),
]


def record_timestamp(record: LogRecord | MetricBucket | ChangeEvent | DependencyRecord | PastIncident) -> datetime | None:
    """The timestamp used for ordering and time filters, or None for dependency records."""
    if isinstance(record, PastIncident):
        return record.occurred_at
    return getattr(record, "timestamp", None)


def record_source_ids(record: LogRecord | MetricBucket | ChangeEvent | DependencyRecord | PastIncident) -> tuple[str, ...]:
    """The record ID followed by its constituent snapshot record IDs (design D9)."""
    return (record.record_id, *getattr(record, "constituent_ids", ()))


class ToolError(StrictModel):
    code: ErrorCode
    message: str
    invalid_fields: tuple[str, ...] = ()


class ToolResult(StrictModel):
    """Output of one Tool_Call or one rejected Tool_Request (Req 13.2, 13.11)."""

    tool: ToolName
    records: tuple[ToolRecord, ...] = ()
    error: ToolError | None = None
    partial: bool = False
    stale: bool = False
    as_of: AwareDatetime | None = None
    truncated: bool = False

    @model_validator(mode="after")
    def _consistent(self) -> "ToolResult":
        if self.error is not None and (self.records or self.partial or self.stale or self.truncated or self.as_of):
            raise ValueError("an error result carries no records and no partial, stale or truncated flag")
        if self.stale != (self.as_of is not None):
            raise ValueError("as_of is set exactly when the stale flag is set")
        return self
