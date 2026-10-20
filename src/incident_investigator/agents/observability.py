"""Observability_Agent: logs and metrics (Req 8)."""

from typing import ClassVar, Literal

from pydantic import BaseModel

from ..llm.base import ValidationContext
from ..models.base import JsonValue, StrictModel, TimeRange
from ..models.outputs import ObservabilityFindings, SpecialistStep
from ..models.tasks import Task
from ..models.vocab import AgentName
from .base import AgentContext
from .opening import OPENING_METRIC, OPENING_STEP_SECONDS, observability_openings, telemetry_window
from .prompts import templates
from .specialist import SpecialistAgent, ToolPlan

__all__ = ["OPENING_METRIC", "OPENING_STEP_SECONDS", "ObservabilityAgent", "check_observability", "opening_window"]


class ObservabilityToolRequest(StrictModel):
    tool: Literal["query_logs", "query_metrics"]
    params: dict[str, JsonValue]


class ObservabilityStep(SpecialistStep[ObservabilityFindings]):
    """One step: a Tool_Request for one of the agent's own Tools, or the findings."""

    tool_request: ObservabilityToolRequest | None = None


def opening_window(ctx: AgentContext) -> TimeRange:
    """``[start - pre_incident_margin, start + OPENING_WINDOW_AFTER)`` (Req 8.2)."""
    return telemetry_window(ctx.incident, ctx.config.orchestrator)


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
        # Shared with the Baseline_Agent's harness (agents/opening.py), so both systems start alike.
        return observability_openings(ctx.incident, ctx.config.orchestrator)

    def check(self, output: BaseModel, vctx: ValidationContext) -> list[str]:
        return check_observability(output, vctx)
