"""Reasoning_Agent: competing, ranked Hypotheses from the Blackboard (Req 1.5, 10). It has no Tool port.

The output model enforces 2-5 Hypotheses, distinct Root_Cause_Labels, ranks 1..N with non-increasing
Confidence, disjoint supporting/contradicting lists and at most 10 missing-evidence entries. The context
rules below add Components from the dependency map and one status per Incident_Report claim.

Hypothesis IDs and the evidence-sufficiency flag are not trusted from the model: after citation
validation the Orchestrator calls ``assign_hypothesis_ids`` (Req 10.8, 10.9; design D11).
"""

from collections.abc import Callable, Sequence
from typing import ClassVar, Protocol

from pydantic import BaseModel

from ..llm.base import ValidationContext
from ..models.evidence import LedgerView
from ..models.hypothesis import HypothesisRevision
from ..models.outputs import HypothesisDraft, ReasoningOutput
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


class HypothesisHistory(Protocol):
    """The Blackboard reads that Hypothesis identity needs."""

    def all_hypothesis_ids(self) -> tuple[str, ...]: ...

    def hypothesis_history(self, hypothesis_id: str) -> tuple[HypothesisRevision, ...]: ...


def assign_hypothesis_ids(
    drafts: Sequence[HypothesisDraft],
    history: HypothesisHistory,
    ledger: LedgerView,
    new_id: Callable[[], str],
) -> list[tuple[str, HypothesisDraft]]:
    """Give each citation-validated draft its Hypothesis ID and the code-enforced sufficiency flag.

    - The ID of the Blackboard Hypothesis with the same Root_Cause_Label, else ``new_id()``, an ID
      never used in the Run (Req 10.8).
    - ``evidence_sufficient`` is forced to false unless a remaining supporting ID is ``complete`` and
      not Historical_Evidence (Req 10.9). It is never raised to true.
    """
    by_label = {
        history.hypothesis_history(hypothesis_id)[-1].label: hypothesis_id
        for hypothesis_id in history.all_hypothesis_ids()
        if history.hypothesis_history(hypothesis_id)
    }
    assigned = []
    for draft in drafts:
        hypothesis_id = by_label.get(draft.label) or new_id()
        sufficient = draft.evidence_sufficient and any(
            ledger.is_complete_current(e) for e in draft.supporting_evidence_ids
        )
        update = {"hypothesis_id": None, "evidence_sufficient": sufficient}
        assigned.append((hypothesis_id, draft.model_copy(update=update)))
    return assigned


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
