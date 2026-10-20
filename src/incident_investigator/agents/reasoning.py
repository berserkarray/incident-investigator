"""Reasoning_Agent: competing, ranked Hypotheses from the Blackboard (Req 1.5, 10). It has no Tool port."""

from typing import ClassVar

from pydantic import BaseModel

from ..llm.base import ValidationContext
from ..models.outputs import ReasoningOutput
from ..models.tasks import Task
from ..models.vocab import AgentName, CallKind
from .base import Agent, AgentContext, AgentOutcome
from .prompts import templates
from .prompts.builders import reasoning_prompt


def check_reasoning(output: BaseModel, vctx: ValidationContext) -> list[str]:
    assert isinstance(output, ReasoningOutput)
    problems = [
        f"hypotheses.{i}.component: {h.component!r} is not in context.components"
        for i, h in enumerate(output.hypotheses)
        if h.component not in vctx.valid_components
    ]
    given = [u.claim_id for u in output.claim_statuses]
    expected = set(vctx.incident_claim_ids)
    unknown = sorted(set(given) - expected)
    missing = sorted(expected - set(given))
    if unknown:
        problems.append(f"claim_statuses: unknown claim IDs {unknown}")
    if missing:
        problems.append(f"claim_statuses: give a status for every claim; missing {missing}")
    if len(given) != len(set(given)):
        problems.append("claim_statuses: each claim appears once")
    return problems


class ReasoningAgent(Agent):
    name: ClassVar[AgentName] = AgentName.REASONING

    async def run(self, task: Task, ctx: AgentContext) -> AgentOutcome:
        vctx = ValidationContext(
            ledger=ctx.board.ledger_view(),
            valid_components=ctx.components,
            incident_claim_ids=tuple(c.claim_id for c in ctx.board.claims()),
            latest_hypotheses=ctx.board.active_hypotheses(),
        )
        user = reasoning_prompt(ctx.board, ctx.incident, ctx.components)
        output = await ctx.llm.structured(
            self.name, CallKind.REASONING, templates.REASONING, user, ReasoningOutput, vctx, check_reasoning
        )
        return AgentOutcome(task_id=task.task_id, agent=self.name, output=output)
