"""One Investigator Run: plan, dispatch, Critic feedback loop, loop end, Verdict and report.

This is the thin-slice Orchestrator: the Default_Task_Graph, concurrent Specialist Tasks, one retry
per failed Task attempt and the Critic loop up to the Critic_Round Budget. The event-driven
scheduler, plan validation, de-duplication, budgets, per-Task timeouts and the wall-clock watchdog
replace parts of it in the orchestrator workstream.
"""

import asyncio
from dataclasses import dataclass

from pydantic import ValidationError

from ..agents.base import Agent, AgentContext, AgentOutcome
from ..agents.critic import CriticAgent
from ..agents.dependency import DependencyAgent
from ..agents.observability import ObservabilityAgent
from ..agents.reasoning import ReasoningAgent
from ..agents.reporting import ReportingAgent
from ..errors import BudgetBlocked, RunClosed
from ..llm.gateway import LLMGateway
from ..llm.validator import StructuredOutputFailure
from ..models.claims import LabelledClaim
from ..models.critic import EvidenceRequest, EvidenceRequestStatus
from ..models.gaps import DataGapDraft
from ..models.outputs import CriticOutput, HypothesisDraft, ReasoningOutput, ReportingOutput
from ..models.report import InvestigationReport, RecommendedAction
from ..models.run import RunInputs, RunResult
from ..models.tasks import Task
from ..models.tools import TOOL_INPUT_MODELS, input_time_range
from ..models.vocab import (
    SPECIALIST_TOOLS,
    AgentName,
    FailureKind,
    SystemName,
    TaskKind,
    TaskState,
    TerminationReason,
)
from ..report.assembler import ReportAssembler, ranked_hypotheses
from ..report.deterministic import DeterministicReportGenerator
from ..report.writer import RunOutputSink
from ..runtime import RunDependencies
from ..state.blackboard import Blackboard
from ..state.citations import CitableOutput, CitationValidator, apply_claim_updates
from ..state.confidence import apply_confidence_policy
from ..state.verdict import investigator_verdict
from ..tools.layer import BoardToolBudget, ScopedToolPort, build_tool_layer
from ..tools.snapshot import PathGuard, SnapshotLoader
from ..tracing import TraceRecorder
from .graph import TaskFactory, default_task_graph

AGENTS: dict[AgentName, Agent] = {
    AgentName.OBSERVABILITY: ObservabilityAgent(),
    AgentName.DEPENDENCY: DependencyAgent(),
    AgentName.REASONING: ReasoningAgent(),
    AgentName.CRITIC: CriticAgent(),
    AgentName.REPORTING: ReportingAgent(),
}


@dataclass
class _LoopEnd:
    reason: TerminationReason
    acceptance: str | None = None


class InvestigatorRun:
    def __init__(self, inputs: RunInputs, deps: RunDependencies, sink: RunOutputSink):
        self.inputs = inputs
        self.deps = deps
        self.sink = sink
        self.config = inputs.config
        self.clock = deps.clock
        self.validator = CitationValidator()
        self.factory = TaskFactory()
        self.tasks: dict[str, Task] = {}
        self._first_task_seen: set[AgentName] = set()

    # setup

    def _setup(self) -> None:
        loader = SnapshotLoader(PathGuard(self.config.snapshot_root))
        self.snapshot = loader.load(self.inputs.snapshot_dir)
        self.run_id = f"{self.snapshot.incident_id}-multi-{self.config.seed}"
        self.trace = TraceRecorder(self.run_id, SystemName.MULTI, self.clock, self.deps.redactor)
        self.board = Blackboard(self.run_id, self.snapshot.incident_id, self.inputs.incident_report, self.clock, self.trace)
        self.limits = self.config.budgets.limits()
        self.layer = build_tool_layer(
            snapshot=self.snapshot,
            config=self.config,
            ledger=self.board,
            gaps=self.board,
            budget=BoardToolBudget(self.board, self.limits.tool_calls),
            trace=self.trace,
            clock=self.clock,
        )
        self.gateway = LLMGateway(self.deps.llm_client, self.board, self.trace, self.limits, self.config.llm.model)

    def _context(self, agent: AgentName) -> AgentContext:
        first = agent not in self._first_task_seen
        self._first_task_seen.add(agent)
        port = ScopedToolPort(self.layer, agent, SPECIALIST_TOOLS[agent]) if agent in SPECIALIST_TOOLS else None
        return AgentContext(
            board=self.board,
            llm=self.gateway,
            tools=port,
            clock=self.clock,
            config=self.config,
            incident=self.inputs.incident_report,
            components=self.snapshot.component_names,
            is_first_task=first,
        )

    # task bookkeeping

    async def _add(self, task: Task) -> Task:
        self.tasks[task.task_id] = task
        await self.board.upsert_task(task)
        return task

    async def _transition(self, task_id: str, to: TaskState, attempt: int | None = None) -> Task:
        task = self.tasks[task_id]
        update = {"state": to} if attempt is None else {"state": to, "attempt": attempt}
        new = task.model_copy(update=update)
        self.tasks[task_id] = new
        await self.board.upsert_task(new)
        self.trace.record("task_transition", task_id=task_id, agent=task.agent, from_state=task.state, to_state=to)
        return new

    async def _run_task(self, task_id: str, *, after_verdict: bool = False) -> AgentOutcome | None:
        await self._transition(task_id, TaskState.RUNNING, attempt=1)
        return await self._execute(task_id, after_verdict=after_verdict)

    async def _run_concurrently(self, task_ids: list[str]) -> list[AgentOutcome | None]:
        """Set every ready Task to running before awaiting any of them (Req 7.1, 7.2, 7.4)."""
        for task_id in task_ids:
            await self._transition(task_id, TaskState.RUNNING, attempt=1)
        return list(await asyncio.gather(*(self._execute(t) for t in task_ids)))

    async def _execute(self, task_id: str, *, after_verdict: bool = False) -> AgentOutcome | None:
        """Run a running Task's attempts: one retry on Agent_Failure (Req 16.1, 16.2)."""
        task = self.tasks[task_id]
        agent = AGENTS[task.agent]
        context = self._context(task.agent)
        for attempt in (1, 2):
            if attempt == 2:
                self.tasks[task_id] = self.tasks[task_id].model_copy(update={"attempt": 2})
            try:
                outcome = await agent.run(self.tasks[task_id], context)
            except (RunClosed, BudgetBlocked):
                await self._transition(task_id, TaskState.CANCELLED)
                return None
            except Exception as exc:  # an Agent_Failure: exception or structured-output failure
                kind = FailureKind.STRUCTURED_OUTPUT if isinstance(exc, StructuredOutputFailure) else FailureKind.EXCEPTION
                self.trace.record(
                    "agent_failure",
                    agent=task.agent,
                    task_id=task_id,
                    attempt=attempt,
                    failure_kind=kind,
                    message=f"{type(exc).__name__}: {exc}"[:2000],
                )
                if attempt == 2:
                    await self._transition(task_id, TaskState.FAILED)
                    await self.board.add_gap(
                        DataGapDraft(source=task.agent, task_id=task_id, reason=kind.value, after_verdict=after_verdict)
                    )
                    return None
                continue
            await self._transition(task_id, TaskState.COMPLETED)
            return outcome
        return None

    # commits (after citation validation)

    async def _commit_findings(self, outcome: AgentOutcome) -> None:
        claims = tuple(getattr(outcome.output, "claims", ()))
        result = self.validator.validate(
            CitableOutput(claims=claims), self.board.ledger_view(), frozenset(), {}, outcome.agent
        )
        self._trace_violations(result.violations)
        await self.board.add_findings(
            [
                LabelledClaim(
                    claim_id=self.board.next_finding_id(),
                    producer=outcome.agent,
                    task_id=outcome.task_id,
                    body=body,
                    label=label,
                )
                for body, label in zip(result.output.claims, result.labels)
            ]
        )

    def _hypothesis_id_for(self, draft: HypothesisDraft) -> str:
        for hypothesis_id in self.board.all_hypothesis_ids():
            latest = self.board.hypothesis_history(hypothesis_id)[-1]
            if latest.label == draft.label:
                return hypothesis_id
        return self.board.next_hypothesis_id()

    async def _commit_reasoning(self, outcome: AgentOutcome) -> None:
        output: ReasoningOutput = outcome.output  # type: ignore[assignment]
        prior = {c.claim_id: c for c in self.board.claims()}
        ledger = self.board.ledger_view()
        result = self.validator.validate(
            CitableOutput(hypotheses=output.hypotheses, claim_updates=output.claim_statuses),
            ledger,
            frozenset(self.board.all_hypothesis_ids()),
            prior,
            outcome.agent,
        )
        self._trace_violations(result.violations)
        pairs = []
        for draft in result.output.hypotheses:
            sufficient = draft.evidence_sufficient and any(
                ledger.is_complete_current(e) for e in draft.supporting_evidence_ids
            )
            pairs.append((self._hypothesis_id_for(draft), draft.model_copy(update={"evidence_sufficient": sufficient})))
        await self.board.add_hypothesis_revisions(outcome.task_id, pairs)
        await self.board.set_claims(apply_claim_updates(prior, result.output.claim_updates))

    def _trace_violations(self, violations) -> None:
        for violation in violations:
            self.trace.record("citation_violation", **violation.trace_fields())

    # the run

    def _tick(self) -> None:
        self.board.set_elapsed(self.clock.monotonic() - self._started)

    async def execute(self) -> RunResult:
        self._started = self.clock.monotonic()
        self._setup()
        self.trace.record(
            "run_started",
            system=SystemName.MULTI,
            incident_id=self.snapshot.incident_id,
            seed=self.config.seed,
            effective_config=self.config.model_dump(mode="json"),
        )
        await self.board.record_initial_claims()
        graph = default_task_graph(self.factory)
        for task in graph:
            await self._add(task)
        observability, dependency, reasoning, critic, reporting = (t.task_id for t in graph)

        end = await self._loop([observability, dependency], reasoning, critic)
        report = await self._finish(end, reporting)
        self._tick()
        self.trace.record(
            "run_ended",
            termination_reason=end.reason,
            critic_rounds={"used": self.board.usage.critic_rounds, "limit": self.limits.critic_rounds},
            tool_calls={"used": self.board.usage.tool_calls, "limit": self.limits.tool_calls},
            input_tokens=self.board.usage.tokens.input_tokens,
            output_tokens=self.board.usage.tokens.output_tokens,
            tokens={"used": self.board.usage.total_tokens, "limit": self.limits.tokens},
            wall_clock_s={"used": self.board.usage.elapsed_s, "limit": self.limits.wall_clock_s},
        )
        trace = self.trace.to_trace()
        ledger = self.board.to_state()
        self.sink.write(report, trace, ledger)
        return RunResult(report=report, trace=trace, ledger=ledger, elapsed_s=self.board.usage.elapsed_s)

    async def _run_specialists(self, task_ids: list[str]) -> None:
        outcomes = await self._run_concurrently(task_ids)
        for task_id, outcome in zip(task_ids, outcomes):
            if outcome is not None:
                await self._commit_findings(outcome)
            request_id = self.tasks[task_id].evidence_request_id
            if request_id is not None:
                status = EvidenceRequestStatus.COMPLETED if outcome is not None else EvidenceRequestStatus.FAILED
                await self._set_request_status(request_id, status)

    async def _set_request_status(self, request_id: str, status: EvidenceRequestStatus) -> None:
        for request in self.board.evidence_requests():
            if request.request_id == request_id:
                await self.board.upsert_evidence_request(request.model_copy(update={"status": status}))

    async def _loop(self, specialists: list[str], reasoning: str, critic: str) -> _LoopEnd:
        budget = self.limits.critic_rounds
        while True:
            await self._run_specialists(specialists)
            outcome = await self._run_task(reasoning)
            if outcome is not None:
                await self._commit_reasoning(outcome)
            if not self.board.active_hypotheses():
                await self._transition(critic, TaskState.CANCELLED)
                return _LoopEnd(TerminationReason.NO_HYPOTHESES)
            critic_round = self.board.increment_critic_round()
            outcome = await self._run_task(critic)
            if outcome is None:
                return _LoopEnd(TerminationReason.CRITIC_FAILED)
            decision: CriticOutput = outcome.output  # type: ignore[assignment]
            requests = self._requests(decision, critic_round)
            self.trace.record(
                "critic_decision",
                critic_round=critic_round,
                accepted_hypothesis_id=decision.accept_hypothesis_id,
                evidence_requests=requests,
            )
            if decision.accept_hypothesis_id is not None:
                return _LoopEnd(TerminationReason.CRITIC_ACCEPTED, decision.accept_hypothesis_id)
            if critic_round >= budget:
                for request in requests:
                    await self.board.upsert_evidence_request(request)
                return _LoopEnd(TerminationReason.MAX_CRITIC_ROUNDS)
            specialists = []
            for request in requests:
                task = await self._add(
                    self.factory.new(
                        request.target,
                        TaskKind.EVIDENCE_REQUEST,
                        evidence_request_id=request.request_id,
                        critic_round=critic_round,
                        focus=request.question,
                    )
                )
                await self.board.upsert_evidence_request(request.model_copy(update={"task_id": task.task_id}))
                specialists.append(task.task_id)
            reasoning = (await self._add(self.factory.new(AgentName.REASONING, TaskKind.REASONING, tuple(specialists)))).task_id
            critic = (
                await self._add(
                    self.factory.new(AgentName.CRITIC, TaskKind.CRITIC, (reasoning,), critic_round=critic_round + 1)
                )
            ).task_id

    def _requests(self, decision: CriticOutput, critic_round: int) -> tuple[EvidenceRequest, ...]:
        requests = []
        for i, draft in enumerate(decision.evidence_requests, start=1):
            params = {k: v for k, v in draft.suggested_params.items() if k != "tool"}
            try:
                parsed = TOOL_INPUT_MODELS[draft.suggested_tool].model_validate({**params, "tool": draft.suggested_tool.value})
            except ValidationError:  # the Critic check already rejects these; keep the run going regardless
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
        return tuple(requests)

    # loop end

    async def _finish(self, end: _LoopEnd, reporting_task: str) -> InvestigationReport:
        for task in list(self.tasks.values()):
            if task.state is TaskState.PENDING and task.task_id != reporting_task:
                await self._transition(task.task_id, TaskState.CANCELLED)
        self._tick()
        record = await self.board.set_termination(end.reason)
        self.trace.record("termination", reason=end.reason, usage=record.usage)

        for request in self.board.evidence_requests():
            if request.status in (EvidenceRequestStatus.TASKED, EvidenceRequestStatus.UNFULFILLED) and (
                request.task_id is None or self.tasks[request.task_id].state is not TaskState.COMPLETED
            ):
                await self.board.upsert_evidence_request(request.model_copy(update={"status": EvidenceRequestStatus.UNFULFILLED}))
                await self.board.add_gap(
                    DataGapDraft(
                        source=request.target,
                        query=request.suggested_params,
                        reason=f"unfulfilled_evidence_request:{request.request_id}:{end.reason}",
                        affected_time_range=input_time_range(request.suggested_params),
                    )
                )

        adjustments = apply_confidence_policy(
            self.board.active_hypotheses(),
            self.board.superseded_hypotheses(),
            self.board.data_gaps(),
            self.config.policy.data_gap_penalty,
        )
        await self.board.set_adjustments(adjustments)
        self.trace.record(
            "confidence_applied",
            adjustments=adjustments,
            gap_count=adjustments[0].gap_count if adjustments else 0,
            penalty=self.config.policy.data_gap_penalty,
        )
        decision = investigator_verdict(
            end.reason,
            end.acceptance,
            ranked_hypotheses(self.board),
            self.board.ledger_view(),
            self.config.policy.conclusive_threshold,
        )
        await self.board.set_verdict(decision.verdict, end.acceptance)
        self.trace.record("verdict_set", verdict=decision.verdict, conditions=decision.conditions)

        assembler = ReportAssembler()
        drg = DeterministicReportGenerator(assembler)
        max_actions = self.config.report.max_recommended_actions
        remaining = self.limits.tokens - self.board.usage.total_tokens
        if remaining < self.limits.reporting_reserve:
            await self._transition(reporting_task, TaskState.CANCELLED)
            self._tick()
            return drg.build(self.board, SystemName.MULTI, self.limits, max_actions)
        # A failed Reporting Task records its Data_Gap after the Verdict; it changes nothing (Req 17.10).
        outcome = await self._run_task(reporting_task, after_verdict=True)
        self._tick()
        if outcome is None:
            return drg.build(self.board, SystemName.MULTI, self.limits, max_actions)
        return self._reporting_report(outcome, assembler)

    def _reporting_report(self, outcome: AgentOutcome, assembler: ReportAssembler) -> InvestigationReport:
        output: ReportingOutput = outcome.output  # type: ignore[assignment]
        result = self.validator.validate(
            CitableOutput(claims=output.summary_claims, actions=output.recommended_actions),
            self.board.ledger_view(),
            frozenset(self.board.all_hypothesis_ids()),
            {},
            AgentName.REPORTING,
        )
        self._trace_violations(result.violations)
        actions = [
            RecommendedAction(
                action_id=f"A{i}",
                text=a.text,
                category=a.category,
                hypothesis_id=a.hypothesis_id,
                evidence_ids=a.evidence_ids,
                producer="reporting_agent",
            )
            for i, a in enumerate(result.output.actions, start=1)
        ]
        extra = [
            LabelledClaim(
                claim_id=self.board.next_finding_id(),
                producer=AgentName.REPORTING,
                task_id=outcome.task_id,
                body=body,
                label=label,
            )
            for body, label in zip(result.output.claims, result.labels)
        ]
        return assembler.assemble(
            self.board,
            system=SystemName.MULTI,
            built_by="reporting_agent",
            actions=actions,
            limits=self.limits,
            extra_claims=extra,
        )


async def run_investigator(inputs: RunInputs, deps: RunDependencies, sink: RunOutputSink) -> RunResult:
    return await InvestigatorRun(inputs, deps, sink).execute()
