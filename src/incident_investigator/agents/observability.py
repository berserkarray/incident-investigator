"""Observability_Agent: logs and metrics (Req 8)."""

from datetime import timedelta
from typing import ClassVar, Literal

from pydantic import BaseModel

from ..llm.base import ValidationContext
from ..models.base import JsonValue, StrictModel, TimeRange
from ..models.outputs import ObservabilityFindings, SpecialistStep
from ..models.tasks import Task
from ..models.vocab import AgentName, LogLevel, MetricName, ToolName
from .base import AgentContext
from .prompts import templates
from .specialist import OPENING_LIMIT, OPENING_WINDOW_AFTER, SpecialistAgent, ToolPlan

OPENING_METRIC = MetricName.ERROR_RATE
OPENING_STEP_SECONDS = 600
"""Opening metrics bucket size: the default 3 h window fits in 18 buckets, under the record limit."""


class ObservabilityToolRequest(StrictModel):
    tool: Literal["query_logs", "query_metrics"]
    params: dict[str, JsonValue]


class ObservabilityStep(SpecialistStep[ObservabilityFindings]):
    """One step: a Tool_Request for one of the agent's own Tools, or the findings."""

    tool_request: ObservabilityToolRequest | None = None


def opening_window(ctx: AgentContext) -> TimeRange:
    """``[start - pre_incident_margin, start + OPENING_WINDOW_AFTER)`` (Req 8.2)."""
    start = ctx.incident.start_time
    margin = ctx.config.orchestrator.pre_incident_margin_s
    return TimeRange(start=start - timedelta(seconds=margin), end=start + OPENING_WINDOW_AFTER)


def check_observability(output: BaseModel, vctx: ValidationContext) -> list[str]:
    """Each claim names one service of the incident (Req 8.3). Signal, onset/no-onset and the
    citation count are enforced by the claim model itself."""
    assert isinstance(output, ObservabilityFindings)
    return [
        f"claims.{i}.service: {c.service!r} is not a component of this incident"
        for i, c in enumerate(output.claims)
        if c.service not in vctx.valid_components
    ]


class ObservabilityAgent(SpecialistAgent):
    name: ClassVar[AgentName] = AgentName.OBSERVABILITY
    findings_model: ClassVar[type[BaseModel]] = ObservabilityFindings
    step_model: ClassVar[type[SpecialistStep]] = ObservabilityStep
    system_prompt: ClassVar[str] = templates.OBSERVABILITY

    def opening_requests(self, task: Task, ctx: AgentContext) -> list[ToolPlan]:
        window = opening_window(ctx).model_dump(mode="json")
        service = ctx.incident.affected_service
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

    def check(self, output: BaseModel, vctx: ValidationContext) -> list[str]:
        return check_observability(output, vctx)
