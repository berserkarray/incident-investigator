"""Dependency_Agent: changes, dependencies and incident history (Req 9)."""

from typing import ClassVar

from pydantic import BaseModel

from ..llm.base import ValidationContext
from ..models.outputs import DependencyFindings
from ..models.tasks import Task
from ..models.vocab import AgentName
from .base import AgentContext
from .prompts import templates
from .specialist import SpecialistAgent, ToolPlan


class DependencyAgent(SpecialistAgent):
    name: ClassVar[AgentName] = AgentName.DEPENDENCY
    findings_model: ClassVar[type[BaseModel]] = DependencyFindings
    system_prompt: ClassVar[str] = templates.DEPENDENCY

    def opening_requests(self, task: Task, ctx: AgentContext) -> list[ToolPlan]:
        # The opening get_deployments and get_dependencies requests are added together with those tools.
        return []

    def check(self, output: BaseModel, vctx: ValidationContext) -> list[str]:
        assert isinstance(output, DependencyFindings)
        return [
            f"claims.{i}.components: {name!r} is not a component of this incident"
            for i, claim in enumerate(output.claims)
            for name in claim.components
            if name not in vctx.valid_components
        ]
