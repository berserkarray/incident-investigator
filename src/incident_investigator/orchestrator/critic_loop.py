"""CriticLoop: turns a validated Critic output into the next step of the feedback loop.

Acceptance ends the loop (critic_accepted). Requests filed in the round equal to the Critic_Round
Budget end it (max_critic_rounds) without new Tasks. Otherwise duplicates are discarded and each kept
request gets one Specialist Task, followed by one Reasoning Task on all of them and one Critic Task on
that (Req 10.4, 11.7-11.14). If every request is a duplicate, the loop ends (duplicate_requests_only).
The "no Hypotheses" case is handled at dispatch, before a Critic Task runs (Req 16.7).
"""

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import ValidationError

from ..llm.validator import StructuredOutputFailure
from ..models.critic import EvidenceRequest, EvidenceRequestStatus
from ..models.outputs import CriticOutput
from ..models.tasks import Task
from ..models.tools import TOOL_INPUT_MODELS, input_time_range
from ..models.vocab import AgentName, TaskKind, TerminationReason
from .graph import TaskFactory


@dataclass(frozen=True)
class Duplicate:
    """A discarded request and what it duplicates: a completed Task or an earlier request of the round."""

    request: EvidenceRequest
    matched_task_id: str | None = None
    matched_request_id: str | None = None


@dataclass(frozen=True)
class LoopAction:
    critic_round: int
    end: TerminationReason | None
    acceptance: str | None
    requests: tuple[EvidenceRequest, ...]  # every filed request with its resolved status
    duplicates: tuple[Duplicate, ...]
    tasks: tuple[Task, ...]  # new Tasks in creation order: Specialists, then Reasoning, then Critic


def same_request(a: EvidenceRequest, b: EvidenceRequest) -> bool:
    """Same target, same Tool and equal parameters after parsing into the Tool input model (Req 11.10).

    Parsed models compare field by field, so key order, explicit defaults, level order and equal
    instants written with different UTC offsets all compare equal."""
    return a.target == b.target and a.suggested_tool == b.suggested_tool and a.suggested_params == b.suggested_params


class CriticLoop:
    def __init__(self, round_budget: int, factory: TaskFactory):
        self.round_budget = round_budget
        self._factory = factory

    @staticmethod
    def build_requests(output: CriticOutput, critic_round: int) -> tuple[EvidenceRequest, ...]:
        """Parse each draft's parameters into its Tool input model. A draft that does not parse fails the
        Critic output (Req 11.4), as the Critic's own validator should already have caught."""
        requests = []
        errors = []
        for i, draft in enumerate(output.evidence_requests, start=1):
            params = {k: v for k, v in draft.suggested_params.items() if k != "tool"}
            try:
                parsed = TOOL_INPUT_MODELS[draft.suggested_tool].model_validate(
                    {**params, "tool": draft.suggested_tool.value}
                )
            except ValidationError as exc:
                fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors(include_url=False))
                errors.append(f"evidence_requests.{i - 1}.suggested_params: invalid ({fields})")
                continue
            requests.append(
                EvidenceRequest(
                    request_id=f"ER{critic_round}.{i}",
                    critic_round=critic_round,
                    target=draft.target,
                    hypothesis_id=draft.hypothesis_id,
                    concern=draft.concern,
                    question=draft.question,
                    suggested_tool=draft.suggested_tool,
                    suggested_params=parsed,
                    rationale=draft.rationale,
                    status=EvidenceRequestStatus.TASKED,
                )
            )
        if errors:
            raise StructuredOutputFailure(errors)
        return tuple(requests)

    @staticmethod
    def dedupe(
        requests: Sequence[EvidenceRequest], completed: Sequence[tuple[str, EvidenceRequest]]
    ) -> tuple[list[EvidenceRequest], list[Duplicate]]:
        """Split a round's requests into kept and duplicates. ``completed`` pairs each completed
        Evidence_Request Task ID with the request it carried. Completed Tasks are matched first."""
        kept: list[EvidenceRequest] = []
        duplicates: list[Duplicate] = []
        earlier: list[EvidenceRequest] = []
        for request in requests:
            match = next(((tid, r) for tid, r in completed if same_request(request, r)), None)
            if match is not None:
                duplicates.append(Duplicate(_as_duplicate(request, match[0]), matched_task_id=match[0]))
            else:
                prior = next((r for r in earlier if same_request(request, r)), None)
                if prior is not None:
                    duplicates.append(
                        Duplicate(_as_duplicate(request, prior.request_id), matched_request_id=prior.request_id)
                    )
                else:
                    kept.append(request)
            earlier.append(request)
        return kept, duplicates

    def on_critic_completed(
        self, task: Task, output: CriticOutput, completed: Sequence[tuple[str, EvidenceRequest]]
    ) -> LoopAction:
        critic_round = task.critic_round or 1
        if output.accept_hypothesis_id is not None:
            return LoopAction(critic_round, TerminationReason.CRITIC_ACCEPTED, output.accept_hypothesis_id, (), (), ())
        filed = self.build_requests(output, critic_round)
        kept, duplicates = self.dedupe(filed, completed)
        by_id = {d.request.request_id: d.request for d in duplicates}

        if critic_round >= self.round_budget:
            # Final round: no Tasks; kept requests stay unfulfilled and get Data_Gaps at loop end (Req 11.13, 17.3).
            resolved = tuple(
                by_id.get(r.request_id) or r.model_copy(update={"status": EvidenceRequestStatus.UNFULFILLED})
                for r in filed
            )
            return LoopAction(critic_round, TerminationReason.MAX_CRITIC_ROUNDS, None, resolved, tuple(duplicates), ())
        if not kept:
            return LoopAction(
                critic_round, TerminationReason.DUPLICATE_REQUESTS_ONLY, None, tuple(by_id[r.request_id] for r in filed), tuple(duplicates), ()
            )

        tasks: list[Task] = []
        tasked: dict[str, EvidenceRequest] = {}
        for request in kept:
            specialist = self._factory.new(
                request.target,
                TaskKind.EVIDENCE_REQUEST,
                evidence_request_id=request.request_id,
                critic_round=critic_round,
                focus=request.question,
                time_range=input_time_range(request.suggested_params),
            )
            tasks.append(specialist)
            tasked[request.request_id] = request.model_copy(update={"task_id": specialist.task_id})
        reasoning = self._factory.new(AgentName.REASONING, TaskKind.REASONING, tuple(t.task_id for t in tasks))
        critic = self._factory.new(AgentName.CRITIC, TaskKind.CRITIC, (reasoning.task_id,))
        tasks.extend((reasoning, critic))
        resolved = tuple(tasked.get(r.request_id) or by_id[r.request_id] for r in filed)
        return LoopAction(critic_round, None, None, resolved, tuple(duplicates), tuple(tasks))


def _as_duplicate(request: EvidenceRequest, matched: str) -> EvidenceRequest:
    return request.model_copy(update={"status": EvidenceRequestStatus.DUPLICATE, "duplicate_of": matched})
