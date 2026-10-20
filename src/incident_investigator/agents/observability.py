"""Observability_Agent: logs and metrics (Req 8)."""

from datetime import timedelta
from typing import ClassVar

from pydantic import BaseModel

from ..llm.base import ValidationContext
from ..models.base import TimeRange
from ..models.outputs import ObservabilityFindings
from ..models.tasks import Task
from ..models.vocab import AgentName, LogLevel, ToolName
from .base import AgentContext
from .prompts import templates
from .specialist import OPENING_LIMIT, OPENING_WINDOW_AFTER, SpecialistAgent, ToolPlan


def opening_window(ctx: AgentContext) -> TimeRange:
    start = ctx.incident.start_time
    margin = ctx.config.orchestrator.pre_incident_margin_s
    return TimeRange(start=start - timedelta(seconds=margin), end=start + OPENING_WINDOW_AFTER)


class ObservabilityAgent(SpecialistAgent):
    name: ClassVar[AgentName] = AgentName.OBSERVABILITY
    findings_model: ClassVar[type[BaseModel]] = ObservabilityFindings
    system_prompt: ClassVar[str] = templates.OBSERVABILITY

    def opening_requests(self, task: Task, ctx: AgentContext) -> list[ToolPlan]:
        window = opening_window(ctx).model_dump(mode="json")
        return [
            (
                ToolName.QUERY_LOGS,
                {
                    "service": ctx.incident.affected_service,
                    "time_range": window,
                    "levels": [LogLevel.WARN.value, LogLevel.ERROR.value],
                    "limit": OPENING_LIMIT,
                },
            ),
        ]

    def check(self, output: BaseModel, vctx: ValidationContext) -> list[str]:
        assert isinstance(output, ObservabilityFindings)
        return [
            f"claims.{i}.service: {c.service!r} is not a component of this incident"
            for i, c in enumerate(output.claims)
            if c.service not in vctx.valid_components
        ]
