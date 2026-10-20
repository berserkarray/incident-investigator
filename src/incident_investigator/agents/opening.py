"""Opening Tool_Requests: the code-issued first requests of an investigation (Req 8.2, 9.2, 9.6; design D10).

Both systems issue exactly these, through the same Tool_Layer and against the same Tool_Call Budget:
the Observability_Agent and the Dependency_Agent in their first Tasks, and the Baseline_Agent's
harness before its first LLM step (Req 25.11). Keeping them in one place is what makes the opening
evidence of the two systems identical.
"""

from datetime import timedelta

from ..models.base import JsonValue, TimeRange
from ..models.config import OrchestratorSettings
from ..models.incident import IncidentReport
from ..models.vocab import AgentName, ChangeType, LogLevel, MetricName, ToolName

OPENING_WINDOW_AFTER = timedelta(hours=2)
"""End of the opening telemetry window after the reported start time (a documented constant, not config)."""

OPENING_LIMIT = 25
"""Record limit of every code-issued Tool_Request (token discipline)."""

OPENING_METRIC = MetricName.ERROR_RATE
OPENING_STEP_SECONDS = 600
"""Opening metrics bucket size: the default 3 h window fits in 18 buckets, under the record limit."""

OPENING_DEPENDENCY_DEPTH = 5
"""Depth of the opening get_dependencies request: the whole upstream and downstream neighbourhood."""

ToolPlan = tuple[ToolName, dict[str, JsonValue]]


def telemetry_window(incident: IncidentReport, settings: OrchestratorSettings) -> TimeRange:
    """``[start - pre_incident_margin, start + OPENING_WINDOW_AFTER)`` (Req 8.2)."""
    start = incident.start_time
    return TimeRange(start=start - timedelta(seconds=settings.pre_incident_margin_s), end=start + OPENING_WINDOW_AFTER)


def change_window(incident: IncidentReport, settings: OrchestratorSettings) -> TimeRange:
    """``[start - change_lookback, start)`` (Req 9.2)."""
    start = incident.start_time
    return TimeRange(start=start - timedelta(seconds=settings.change_lookback_s), end=start)


def observability_openings(incident: IncidentReport, settings: OrchestratorSettings) -> list[ToolPlan]:
    """Logs and the error-rate metric of the affected service over the telemetry window."""
    window = telemetry_window(incident, settings).model_dump(mode="json")
    service = incident.affected_service
    return [
        (
            ToolName.QUERY_LOGS,
            {
                "service": service,
                "time_range": window,
                "levels": [LogLevel.WARN.value, LogLevel.ERROR.value],
                "limit": OPENING_LIMIT,
            },
        ),
        (
            ToolName.QUERY_METRICS,
            {
                "service": service,
                "metric_name": OPENING_METRIC.value,
                "time_range": window,
                "step_seconds": OPENING_STEP_SECONDS,
                "limit": OPENING_LIMIT,
            },
        ),
    ]


def dependency_openings(incident: IncidentReport, settings: OrchestratorSettings) -> list[ToolPlan]:
    """Every change to the affected service over the change lookback, and its dependency neighbourhood."""
    service = incident.affected_service
    return [
        (
            ToolName.GET_DEPLOYMENTS,
            {
                "service": service,
                "time_range": change_window(incident, settings).model_dump(mode="json"),
                "change_types": [c.value for c in ChangeType],
                "limit": OPENING_LIMIT,
            },
        ),
        (
            ToolName.GET_DEPENDENCIES,
            {"service": service, "depth": OPENING_DEPENDENCY_DEPTH, "limit": OPENING_LIMIT},
        ),
    ]


def opening_plan(incident: IncidentReport, settings: OrchestratorSettings) -> list[tuple[AgentName, ToolName, dict[str, JsonValue]]]:
    """All opening requests in order, each tagged with the Specialist_Agent that makes it in the Investigator."""
    return [
        *((AgentName.OBSERVABILITY, tool, params) for tool, params in observability_openings(incident, settings)),
        *((AgentName.DEPENDENCY, tool, params) for tool, params in dependency_openings(incident, settings)),
    ]
