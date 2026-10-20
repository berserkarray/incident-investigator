"""Shared behaviour of the two Specialist_Agents (Req 8, 9).

Code issues the mandatory first Tool_Requests (design D10); tool errors come back as observations
and never raise (Req 8.6, 9.8, 17.8). The step loop that lets the model choose further requests is
added with the full agents.
"""

from collections.abc import Sequence
from datetime import timedelta
from typing import Any, ClassVar

from pydantic import BaseModel

from ..llm.base import ValidationContext
from ..models.base import JsonValue
from ..models.evidence import Observation
from ..models.tasks import Task
from ..models.vocab import SPECIALIST_TOOLS, CallKind, ToolName
from .base import Agent, AgentContext, AgentOutcome
from .prompts.builders import specialist_prompt

OPENING_WINDOW_AFTER = timedelta(hours=2)
"""End of the opening query window after the reported start time (a documented constant, not config)."""

OPENING_LIMIT = 25

ToolPlan = tuple[ToolName, dict[str, JsonValue]]


class SpecialistAgent(Agent):
    findings_model: ClassVar[type[BaseModel]]
    system_prompt: ClassVar[str]

    @property
    def tools(self) -> frozenset[ToolName]:
        return SPECIALIST_TOOLS[self.name]

    def opening_requests(self, task: Task, ctx: AgentContext) -> list[ToolPlan]:
        """Tool_Requests code issues for the first Task of the Run (Req 8.2, 9.2, 9.6)."""
        return []

    def check(self, output: BaseModel, vctx: ValidationContext) -> list[str]:
        return []

    def _evidence_request_plan(self, task: Task, ctx: AgentContext) -> list[ToolPlan]:
        if task.evidence_request_id is None:
            return []
        for request in ctx.board.evidence_requests():
            if request.request_id == task.evidence_request_id:
                params = request.suggested_params.model_dump(mode="json")
                params.pop("tool", None)
                return [(request.suggested_tool, params)]
        return []

    def _task_entry(self, task: Task, ctx: AgentContext) -> dict[str, Any]:
        entry: dict[str, Any] = {"task_id": task.task_id, "kind": task.kind.value, "focus": task.brief.focus}
        for request in ctx.board.evidence_requests():
            if request.request_id == task.evidence_request_id:
                entry["evidence_request"] = {
                    "request_id": request.request_id,
                    "hypothesis_id": request.hypothesis_id,
                    "concern": request.concern.value,
                    "question": request.question,
                }
        return entry

    async def run(self, task: Task, ctx: AgentContext) -> AgentOutcome:
        plan = self._evidence_request_plan(task, ctx) or (self.opening_requests(task, ctx) if ctx.is_first_task else [])
        step_limit = ctx.config.orchestrator.per_task_step_limit
        observations: list[Observation] = []
        if ctx.tools is not None:
            for tool, params in plan[:step_limit]:
                observations.append(await ctx.tools.request(task.task_id, tool, params))
        findings = await self._findings(task, ctx, observations, step_limit - len(observations))
        return AgentOutcome(task_id=task.task_id, agent=self.name, output=findings, tool_requests=len(observations))

    async def _findings(
        self, task: Task, ctx: AgentContext, observations: Sequence[Observation], steps_remaining: int
    ) -> BaseModel:
        if not any(o.evidence_id for o in observations):
            return self.findings_model()  # nothing citable: no claims, no LLM call
        vctx = ValidationContext(ledger=ctx.board.ledger_view(), valid_components=ctx.components)
        user = specialist_prompt(
            role=self.name.value,
            incident=ctx.incident,
            components=ctx.components,
            task=self._task_entry(task, ctx),
            observations=observations,
            tools=sorted(self.tools),
            steps_remaining=max(0, steps_remaining),
        )
        return await ctx.llm.structured(
            self.name, CallKind.SPECIALIST_FINDINGS, self.system_prompt, user, self.findings_model, vctx, self.check
        )
