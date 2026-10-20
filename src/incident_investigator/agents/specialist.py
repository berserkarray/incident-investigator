"""Shared behaviour of the two Specialist_Agents (Req 8, 9; design D10).

One Task runs in this order:

1. Code-issued Tool_Requests: for a Task created from an Evidence_Request, the suggested Tool with the
   suggested parameters comes first (Req 8.7, 9.7); the agent's first Task of the Run adds the mandatory
   opening requests (Req 8.2, 9.2, 9.6).
2. The ``specialist_step`` loop: each step is one LLM call that returns either one Tool_Request or the
   findings. Every Tool_Request is one step, including code-issued and rejected ones (Req 8.4, 9.5).
3. At the step limit without findings, one ``specialist_findings`` call returns them (Req 8.5, 9.5).

Tool errors come back as Observations and go into the next step; they never raise (Req 8.6, 9.8, 17.8).
The step model's ``tool`` field is a ``Literal`` of the agent's own Tools, so a disallowed Tool fails
validation and is repaired; the ScopedToolPort is a second guard (Req 8.1, 9.1).
"""

from collections.abc import Sequence
from typing import Any, ClassVar

from pydantic import BaseModel

from ..llm.base import ValidationContext
from ..models.base import JsonValue
from ..models.evidence import Observation
from ..models.hypothesis import HypothesisRevision
from ..models.outputs import SpecialistStep
from ..models.tasks import Task
from ..models.vocab import SPECIALIST_TOOLS, CallKind, ToolName
from .base import Agent, AgentContext, AgentOutcome
from .opening import OPENING_LIMIT, OPENING_WINDOW_AFTER, ToolPlan
from .prompts.builders import specialist_prompt

__all__ = ["OPENING_LIMIT", "OPENING_WINDOW_AFTER", "SpecialistAgent", "ToolPlan"]


def _hypothesis(ctx: AgentContext, hypothesis_id: str) -> HypothesisRevision | None:
    for h in (*ctx.board.active_hypotheses(), *ctx.board.superseded_hypotheses()):
        if h.hypothesis_id == hypothesis_id:
            return h
    return None


class SpecialistAgent(Agent):
    findings_model: ClassVar[type[BaseModel]]
    step_model: ClassVar[type[SpecialistStep]]
    system_prompt: ClassVar[str]
    extra_vocabularies: ClassVar[dict[str, list[str]]] = {}

    @property
    def tools(self) -> frozenset[ToolName]:
        return SPECIALIST_TOOLS[self.name]

    # hooks

    def opening_requests(self, task: Task, ctx: AgentContext) -> list[ToolPlan]:
        """Tool_Requests code issues in the agent's first Task of the Run (Req 8.2, 9.2, 9.6)."""
        return []

    def check(self, output: BaseModel, vctx: ValidationContext) -> list[str]:
        """Context rules for the findings model; each message reads ``loc: problem``."""
        return []

    # plan and prompt content

    def code_issued_requests(self, task: Task, ctx: AgentContext) -> list[ToolPlan]:
        """The suggested request of an Evidence_Request Task first, then the opening requests of the first Task."""
        plan = self._evidence_request_plan(task, ctx)
        if ctx.is_first_task:
            plan += self.opening_requests(task, ctx)
        return plan

    def _evidence_request_plan(self, task: Task, ctx: AgentContext) -> list[ToolPlan]:
        if task.evidence_request_id is None:
            return []
        for request in ctx.board.evidence_requests():
            if request.request_id == task.evidence_request_id and request.suggested_tool in self.tools:
                params = request.suggested_params.model_dump(mode="json")
                params.pop("tool", None)
                return [(request.suggested_tool, params)]
        return []

    def _task_entry(self, task: Task, ctx: AgentContext) -> dict[str, Any]:
        entry: dict[str, Any] = {"task_id": task.task_id, "kind": task.kind.value, "focus": task.brief.focus}
        if task.brief.time_range is not None:
            entry["time_range"] = task.brief.time_range.model_dump(mode="json")
        for request in ctx.board.evidence_requests():
            if request.request_id == task.evidence_request_id:
                hypothesis = _hypothesis(ctx, request.hypothesis_id)
                entry["evidence_request"] = {
                    "request_id": request.request_id,
                    "hypothesis_id": request.hypothesis_id,
                    "hypothesis": None
                    if hypothesis is None
                    else {
                        "component": hypothesis.component,
                        "cause_type": hypothesis.cause_type.value,
                        "statement": hypothesis.statement,
                    },
                    "concern": request.concern.value,
                    "question": request.question,
                    "rationale": request.rationale,
                    "suggested_tool": request.suggested_tool.value,
                }
        return entry

    def _prompt(
        self,
        task: Task,
        ctx: AgentContext,
        observations: Sequence[Observation],
        notes: Sequence[str],
        steps_remaining: int,
        *,
        final: bool,
    ) -> str:
        own = {o.evidence_id for o in observations if o.evidence_id}
        return specialist_prompt(
            role=self.name.value,
            incident=ctx.incident,
            components=ctx.components,
            task=self._task_entry(task, ctx),
            observations=observations,
            tools=sorted(self.tools),
            steps_remaining=max(0, steps_remaining),
            notes=notes,
            other_evidence=[e for e in ctx.board.evidence() if e.evidence_id not in own],
            extra_vocabularies=self.extra_vocabularies,
            final=final,
        )

    def _vctx(self, ctx: AgentContext) -> ValidationContext:
        return ValidationContext(
            ledger=ctx.board.ledger_view(), valid_components=ctx.components, allowed_tools=self.tools
        )

    def _step_check(self, output: BaseModel, vctx: ValidationContext) -> list[str]:
        assert isinstance(output, SpecialistStep)
        if output.findings is None:
            return []
        return [f"findings.{problem}" for problem in self.check(output.findings, vctx)]

    # the Task

    async def run(self, task: Task, ctx: AgentContext) -> AgentOutcome:
        step_limit = ctx.config.orchestrator.per_task_step_limit
        observations: list[Observation] = []
        notes: list[str] = []

        async def request(tool: ToolName | str, params: dict[str, JsonValue]) -> None:
            assert ctx.tools is not None
            observations.append(await ctx.tools.request(task.task_id, tool, params))

        if ctx.tools is None:  # not expected for a Specialist; nothing to query, nothing to cite
            return AgentOutcome(task_id=task.task_id, agent=self.name, output=self.findings_model())

        for tool, params in self.code_issued_requests(task, ctx)[:step_limit]:
            await request(tool, params)

        while len(observations) < step_limit:
            step = await ctx.llm.structured(
                self.name,
                CallKind.SPECIALIST_STEP,
                self.system_prompt,
                self._prompt(task, ctx, observations, notes, step_limit - len(observations), final=False),
                self.step_model,
                self._vctx(ctx),
                self._step_check,
            )
            if step.findings is not None:
                return self._outcome(task, step.findings, observations)
            notes.append(step.reasoning)
            await request(step.tool_request.tool, dict(step.tool_request.params))

        findings = await self._final_findings(task, ctx, observations, notes)
        return self._outcome(task, findings, observations)

    async def _final_findings(
        self, task: Task, ctx: AgentContext, observations: Sequence[Observation], notes: Sequence[str]
    ) -> BaseModel:
        """Findings at the step limit (Req 8.5, 9.5). With nothing citable there is nothing to ask the model."""
        if not any(o.evidence_id for o in observations):
            return self.findings_model()
        return await ctx.llm.structured(
            self.name,
            CallKind.SPECIALIST_FINDINGS,
            self.system_prompt,
            self._prompt(task, ctx, observations, notes, 0, final=True),
            self.findings_model,
            self._vctx(ctx),
            self.check,
        )

    def _outcome(self, task: Task, findings: BaseModel, observations: Sequence[Observation]) -> AgentOutcome:
        return AgentOutcome(task_id=task.task_id, agent=self.name, output=findings, tool_requests=len(observations))
