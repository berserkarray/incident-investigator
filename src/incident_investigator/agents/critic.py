"""Critic_Agent: accept the top-ranked Hypothesis or file Evidence_Requests (Req 11). It has no Tool port."""

from typing import ClassVar

from pydantic import BaseModel, ValidationError

from ..llm.base import ValidationContext
from ..models.outputs import CriticOutput
from ..models.tasks import Task
from ..models.tools import TOOL_INPUT_MODELS, input_service
from ..models.vocab import SPECIALIST_TOOLS, AgentName, CallKind, ConcernCategory
from .base import Agent, AgentContext, AgentOutcome
from .prompts import templates
from .prompts.builders import critic_prompt


def check_critic(output: BaseModel, vctx: ValidationContext) -> list[str]:
    assert isinstance(output, CriticOutput)
    problems: list[str] = []
    ranked = sorted(vctx.latest_hypotheses, key=lambda h: h.rank)
    rank1 = ranked[0] if ranked else None
    active_ids = {h.hypothesis_id for h in ranked}

    if output.accept_hypothesis_id is not None:
        if rank1 is None or output.accept_hypothesis_id != rank1.hypothesis_id:
            problems.append("accept_hypothesis_id: must name the rank-1 hypothesis")
    if len(output.evidence_requests) > vctx.max_evidence_requests:
        problems.append(f"evidence_requests: at most {vctx.max_evidence_requests} requests per round")
    for i, request in enumerate(output.evidence_requests):
        if request.hypothesis_id not in active_ids:
            problems.append(f"evidence_requests.{i}.hypothesis_id: {request.hypothesis_id!r} is not an active hypothesis")
        if request.suggested_tool not in SPECIALIST_TOOLS.get(request.target, frozenset()):
            problems.append(
                f"evidence_requests.{i}.suggested_tool: {request.suggested_tool} is not a tool of {request.target}"
            )
            continue
        params = {k: v for k, v in request.suggested_params.items() if k != "tool"}
        try:
            parsed = TOOL_INPUT_MODELS[request.suggested_tool].model_validate(
                {**params, "tool": request.suggested_tool.value}
            )
        except ValidationError as exc:
            fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors(include_url=False))
            problems.append(f"evidence_requests.{i}.suggested_params: invalid input for {request.suggested_tool} ({fields})")
            continue
        service = input_service(parsed)
        if service is not None and vctx.valid_components and service not in vctx.valid_components:
            problems.append(f"evidence_requests.{i}.suggested_params.service: {service!r} is not a component")

    if rank1 is not None:
        cited = [e for e in rank1.supporting_evidence_ids if vctx.ledger.has(e)]
        current = [e for e in cited if not vctx.ledger.is_historical(e)]
        historical = [e for e in cited if vctx.ledger.is_historical(e)]
        complete = [e for e in current if vctx.ledger.is_complete_current(e)]
        concerns = {
            r.concern for r in output.evidence_requests if r.hypothesis_id == rank1.hypothesis_id
        }
        if not current:
            if output.accept_hypothesis_id is not None:
                problems.append("accept_hypothesis_id: the top hypothesis has no support beyond past incidents")
            if historical and ConcernCategory.HISTORICAL_ONLY_SUPPORT not in concerns:
                problems.append("evidence_requests: file a historical_only_support request for the top hypothesis")
        elif not complete:
            if output.accept_hypothesis_id is not None:
                problems.append("accept_hypothesis_id: the top hypothesis has only partial or stale support")
            if ConcernCategory.LOW_RELIABILITY_SUPPORT not in concerns:
                problems.append("evidence_requests: file a low_reliability_support request for the top hypothesis")
    return problems


class CriticAgent(Agent):
    name: ClassVar[AgentName] = AgentName.CRITIC

    async def run(self, task: Task, ctx: AgentContext) -> AgentOutcome:
        orchestration = ctx.config.orchestrator
        vctx = ValidationContext(
            ledger=ctx.board.ledger_view(),
            valid_components=ctx.components,
            latest_hypotheses=ctx.board.active_hypotheses(),
            max_evidence_requests=orchestration.max_evidence_requests_per_round,
        )
        user = critic_prompt(
            ctx.board,
            ctx.incident,
            ctx.components,
            critic_round=task.critic_round or 1,
            round_budget=ctx.config.budgets.critic_rounds,
            max_requests=orchestration.max_evidence_requests_per_round,
        )
        output = await ctx.llm.structured(self.name, CallKind.CRITIC, templates.CRITIC, user, CriticOutput, vctx, check_critic)
        return AgentOutcome(task_id=task.task_id, agent=self.name, output=output)
