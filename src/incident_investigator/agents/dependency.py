"""Dependency_Agent: changes, dependencies and incident history (Req 9)."""

from typing import ClassVar, Literal

from pydantic import BaseModel

from ..llm.base import ValidationContext
from ..models.base import JsonValue, StrictModel, TimeRange
from ..models.outputs import DependencyFindings, SpecialistStep
from ..models.tasks import Task
from ..models.vocab import AgentName, FindingType
from .base import AgentContext
from .opening import OPENING_DEPENDENCY_DEPTH, change_window, dependency_openings
from .prompts import templates
from .specialist import SpecialistAgent, ToolPlan

__all__ = ["OPENING_DEPENDENCY_DEPTH", "DependencyAgent", "check_dependency", "lookback_window"]


class DependencyToolRequest(StrictModel):
    tool: Literal["get_deployments", "get_dependencies", "search_incidents"]
    params: dict[str, JsonValue]


class DependencyStep(SpecialistStep[DependencyFindings]):
    """One step: a Tool_Request for one of the agent's own Tools, or the findings."""

    tool_request: DependencyToolRequest | None = None


def lookback_window(ctx: AgentContext) -> TimeRange:
    """``[start - change_lookback, start)`` (Req 9.2)."""
    return change_window(ctx.incident, ctx.config.orchestrator)


def check_dependency(output: BaseModel, vctx: ValidationContext) -> list[str]:
    """Components belong to the incident, and the historical rule of Req 9.4: a claim is historical
    exactly when every citation is search_incidents evidence, and a claim citing any search_incidents
    evidence must be historical. Finding type and event-time presence are enforced by the claim model."""
    assert isinstance(output, DependencyFindings)
    problems: list[str] = []
    for i, claim in enumerate(output.claims):
        problems += [
            f"claims.{i}.components: {name!r} is not a component of this incident"
            for name in claim.components
            if name not in vctx.valid_components
        ]
        historical_ids = [e for e in claim.evidence_ids if vctx.ledger.is_historical(e)]
        every_cited_is_history = len(historical_ids) == len(claim.evidence_ids)
        if claim.historical and not every_cited_is_history:
            problems.append(
                f"claims.{i}.evidence_ids: a historical (past_incident) claim cites only search_incidents evidence"
            )
        elif not claim.historical and historical_ids:
            problems.append(
                f"claims.{i}.historical: the claim cites search_incidents evidence {historical_ids}; make it a "
                "past_incident claim with historical=true that cites only search_incidents evidence, or drop "
                "those citations"
            )
    return problems


class DependencyAgent(SpecialistAgent):
    name: ClassVar[AgentName] = AgentName.DEPENDENCY
    findings_model: ClassVar[type[BaseModel]] = DependencyFindings
    step_model: ClassVar[type[SpecialistStep]] = DependencyStep
    system_prompt: ClassVar[str] = templates.DEPENDENCY
    extra_vocabularies: ClassVar[dict[str, list[str]]] = {"finding_type": [f.value for f in FindingType]}

    def opening_requests(self, task: Task, ctx: AgentContext) -> list[ToolPlan]:
        # Shared with the Baseline_Agent's harness (agents/opening.py), so both systems start alike.
        return dependency_openings(ctx.incident, ctx.config.orchestrator)

    def check(self, output: BaseModel, vctx: ValidationContext) -> list[str]:
        return check_dependency(output, vctx)
