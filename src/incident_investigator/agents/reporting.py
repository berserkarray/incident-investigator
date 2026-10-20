"""Reporting_Agent: recommended actions and summary claims (Req 12). It has no Tool port.

The Verdict and Termination_Reason are decided by the Orchestrator and copied unchanged (Req 19.7).
"""

from typing import ClassVar

from pydantic import BaseModel

from ..llm.base import ValidationContext
from ..models.outputs import ReportingOutput
from ..models.tasks import Task
from ..models.vocab import AgentName, CallKind
from ..report.assembler import missing_evidence_entries, to_report_hypotheses
from ..state.citations import unsupported_action_problems
from .base import Agent, AgentContext, AgentOutcome
from .prompts import templates
from .prompts.builders import reporting_prompt


def check_reporting(output: BaseModel, vctx: ValidationContext) -> list[str]:
    assert isinstance(output, ReportingOutput)
    known = {h.hypothesis_id for h in vctx.latest_hypotheses}
    problems = [
        f"recommended_actions.{i}.hypothesis_id: {a.hypothesis_id!r} is not a hypothesis on the board"
        for i, a in enumerate(output.recommended_actions)
        if a.hypothesis_id not in known
    ]
    if len(output.recommended_actions) > vctx.max_actions:
        problems.append(f"recommended_actions: at most {vctx.max_actions} actions")
    problems.extend(unsupported_action_problems(output.recommended_actions, vctx.ledger))
    return problems


class ReportingAgent(Agent):
    name: ClassVar[AgentName] = AgentName.REPORTING

    async def run(self, task: Task, ctx: AgentContext) -> AgentOutcome:
        board = ctx.board
        max_actions = ctx.config.report.max_recommended_actions
        active, superseded = board.active_hypotheses(), board.superseded_hypotheses()
        vctx = ValidationContext(
            ledger=board.ledger_view(),
            valid_components=ctx.components,
            latest_hypotheses=(*active, *superseded),
            max_actions=max_actions,
        )
        user = reporting_prompt(
            board,
            ctx.incident,
            verdict=str(board.verdict),
            termination_reason=str(board.termination.reason) if board.termination else "",
            ranked=to_report_hypotheses(active, board.adjustments()),
            superseded=to_report_hypotheses(superseded, board.adjustments()),
            missing_evidence=[m.model_dump(mode="json") for m in missing_evidence_entries(board)],
            max_actions=max_actions,
        )
        output = await ctx.llm.structured(
            self.name, CallKind.REPORTING, templates.REPORTING, user, ReportingOutput, vctx, check_reporting
        )
        return AgentOutcome(task_id=task.task_id, agent=self.name, output=output)
