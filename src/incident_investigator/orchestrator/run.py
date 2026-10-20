"""One Investigator Run: plan, dispatch, Critic feedback loop, loop end, Verdict and report (Req 6, 7, 11, 16-19).

Flow: load the snapshot, record the Incident_Report claims, plan (or fall back to the
Default_Task_Graph), then let the Scheduler dispatch Ready_Tasks. Specialist, Reasoning and Critic
outcomes are committed after citation validation. The TerminationArbiter records exactly one
Termination_Reason; at that moment the loop stops synchronously (Tool_Layer closed, non-Reporting
LLM calls refused, non-Reporting Tasks cancelled). The loop-end steps then run in design order:
unfulfilled-request Data_Gaps, the usage snapshot, the Confidence_Policy once, the Verdict once, and
the report path (Reporting_Agent with one retry, or the Deterministic_Report_Generator).

A watchdog sleeps on the Injectable_Clock for the wall-clock timeout. If it fires before the report
is built, the whole investigation is cancelled, every gate is closed, the remaining loop-end steps
run without any await on a Tool or an LLM, and the Deterministic_Report_Generator builds the report.
"""

import asyncio
from collections.abc import Mapping
from contextlib import suppress
from typing import Any

from ..agents.base import Agent, AgentContext, AgentOutcome
from ..agents.critic import CriticAgent
from ..agents.dependency import DependencyAgent
from ..agents.observability import ObservabilityAgent
from ..agents.reasoning import ReasoningAgent, assign_hypothesis_ids
from ..agents.reporting import ReportingAgent
from ..clock import ClockTimeout
from ..llm.gateway import LLMGateway
from ..models.base import JsonValue
from ..models.claims import LabelledClaim
from ..models.critic import EvidenceRequest, EvidenceRequestStatus
from ..models.evidence import Observation
from ..models.gaps import DataGapDraft
from ..models.outputs import CriticOutput, ReasoningOutput, ReportingOutput
from ..models.report import InvestigationReport, RecommendedAction
from ..models.run import RunInputs, RunResult
from ..models.tasks import Task
from ..models.tools import input_time_range
from ..models.vocab import (
    SPECIALIST_TOOLS,
    AgentName,
    SystemName,
    TaskKind,
    TaskState,
    TerminationReason,
    ToolName,
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
from ..tools.ports import ToolPort
from ..tools.snapshot import PathGuard, SnapshotLoader
from ..tracing import TraceRecorder
from .critic_loop import CriticLoop
from .graph import TaskFactory, TaskGraph
from .planner import Planner
from .scheduler import Scheduler
from .termination import CheckPoint, TerminationArbiter

AGENTS: dict[AgentName, Agent] = {
    AgentName.OBSERVABILITY: ObservabilityAgent(),
    AgentName.DEPENDENCY: DependencyAgent(),
    AgentName.REASONING: ReasoningAgent(),
    AgentName.CRITIC: CriticAgent(),
    AgentName.REPORTING: ReportingAgent(),
}

SPECIALIST_KINDS = frozenset({TaskKind.INITIAL, TaskKind.EVIDENCE_REQUEST})


class _DeferredToolSignal:
    """Given to the Tool_Layer: a budget_exhausted rejection raises the condition, and the check runs
    once the Tool_Result has been returned (and its Data_Gap recorded), per design D13."""

    def __init__(self, arbiter: TerminationArbiter):
        self._arbiter = arbiter

    def signal(self, reason: TerminationReason) -> None:
        self._arbiter.signal(reason, point=CheckPoint.TOOL_RESULT, check=False)


class _CheckedToolPort:
    """A Specialist's Tool port that runs the termination check after every Tool_Result."""

    def __init__(self, port: ToolPort, arbiter: TerminationArbiter):
        self._port = port
        self._arbiter = arbiter

    async def request(self, task_id: str | None, tool: ToolName | str, params: Mapping[str, JsonValue]) -> Observation:
        try:
            return await self._port.request(task_id, tool, params)
        finally:
            self._arbiter.check(CheckPoint.TOOL_RESULT)


class InvestigatorRun:
    def __init__(self, inputs: RunInputs, deps: RunDependencies, sink: RunOutputSink):
        self.inputs = inputs
        self.deps = deps
        self.sink = sink
        self.config = inputs.config
        self.clock = deps.clock
        self.validator = CitationValidator()
        self.factory = TaskFactory()
        self._agents_started: set[AgentName] = set()
        self._first_task: dict[str, bool] = {}
        self._pending_end: tuple[TerminationReason, str | None] | None = None
        self._acceptance: str | None = None
        self._reporting_outcome: AgentOutcome | None = None
        self._gaps_recorded = False
        self._confidence_applied = False

    # setup

    def _setup(self) -> None:
        loader = SnapshotLoader(PathGuard(self.config.snapshot_root))
        self.snapshot = loader.load(self.inputs.snapshot_dir)  # before any Tool_Call or LLM call (Req 2.2, 2.4)
        self.run_id = f"{self.snapshot.incident_id}-multi-{self.config.seed}"
        self.trace = TraceRecorder(self.run_id, SystemName.MULTI, self.clock, self.deps.redactor)
        self.board = Blackboard(self.run_id, self.snapshot.incident_id, self.inputs.incident_report, self.clock, self.trace)
        self.limits = self.config.budgets.limits()
        self.arbiter = TerminationArbiter(
            tokens_used=lambda: self.board.usage.total_tokens,
            token_threshold=self.limits.tokens - self.limits.reporting_reserve,
            elapsed=self._elapsed,
            wall_clock_s=self.limits.wall_clock_s,
            on_end=self._on_loop_end,
        )
        # The full pipeline: Failure_Injector, Retry_Policy and Circuit_Breaker from the config (Req 14, 15).
        self.layer = build_tool_layer(
            snapshot=self.snapshot,
            config=self.config,
            ledger=self.board,
            gaps=self.board,
            budget=BoardToolBudget(self.board, self.limits.tool_calls),
            trace=self.trace,
            clock=self.clock,
            termination=_DeferredToolSignal(self.arbiter),
        )
        self.gateway = LLMGateway(
            self.deps.llm_client, self.board, self.trace, self.limits, self.config.llm.model, termination=self.arbiter
        )
        self.scheduler = Scheduler(
            TaskGraph(),
            hooks=self,
            board=self.board,
            trace=self.trace,
            clock=self.clock,
            concurrency_limit=self.config.orchestrator.concurrency_limit,
            per_task_timeout_s=self.config.orchestrator.per_task_timeout_s,
            loop_ended=lambda: self.arbiter.ended,
        )
        self.critic_loop = CriticLoop(self.limits.critic_rounds, self.factory)
        self.planner = Planner(self.gateway, self.trace, self.config.orchestrator, self.snapshot.component_names)

    def _elapsed(self) -> float:
        return self.clock.monotonic() - self._started

    def _tick(self) -> None:
        self.board.set_elapsed(self._elapsed())

    @property
    def graph(self) -> TaskGraph:
        return self.scheduler.graph

    # scheduler hooks

    def agent_for(self, task: Task) -> Agent:
        return AGENTS[task.agent]

    def context_for(self, task: Task) -> AgentContext:
        tools: ToolPort | None = None
        if task.agent in SPECIALIST_TOOLS:
            tools = _CheckedToolPort(ScopedToolPort(self.layer, task.agent, SPECIALIST_TOOLS[task.agent]), self.arbiter)
        return AgentContext(
            board=self.board,
            llm=self.gateway,
            tools=tools,
            clock=self.clock,
            config=self.config,
            incident=self.inputs.incident_report,
            components=self.snapshot.component_names,
            is_first_task=self._first_task.get(task.task_id, False),
        )

    def admit(self, task: Task) -> bool:
        # A Critic Task that becomes ready with no Hypotheses is cancelled without a Critic_Round (Req 16.7).
        return not (task.kind is TaskKind.CRITIC and not self.board.active_hypotheses())

    def on_rejected(self, task: Task) -> None:
        if task.kind is TaskKind.CRITIC:
            self.arbiter.signal(TerminationReason.NO_HYPOTHESES, point=CheckPoint.TASK_TERMINAL)

    def on_start(self, task: Task) -> dict[str, Any]:
        self._first_task[task.task_id] = task.agent not in self._agents_started
        self._agents_started.add(task.agent)
        if task.kind is TaskKind.CRITIC:
            # The round counter moves at dispatch; a retried attempt stays in the same round (Req 16.1).
            return {"critic_round": self.board.increment_critic_round()}
        return {}

    async def on_success(self, task: Task, outcome: AgentOutcome) -> None:
        if task.kind in SPECIALIST_KINDS:
            await self._commit_findings(outcome)
        elif task.kind is TaskKind.REASONING:
            await self._commit_reasoning(outcome)
        elif task.kind is TaskKind.CRITIC:
            self._commit_critic(task, outcome)
        elif task.kind is TaskKind.REPORTING:
            self._reporting_outcome = outcome

    async def after_terminal(self, task: Task) -> None:
        if task.evidence_request_id is not None and task.state in (TaskState.COMPLETED, TaskState.FAILED):
            request = self.board.evidence_request(task.evidence_request_id)
            if request is not None:
                status = EvidenceRequestStatus.COMPLETED if task.state is TaskState.COMPLETED else EvidenceRequestStatus.FAILED
                self.board.put_evidence_request(request.model_copy(update={"status": status}))
        if task.kind is TaskKind.CRITIC:
            if task.state is TaskState.FAILED:
                self.arbiter.signal(TerminationReason.CRITIC_FAILED, point=CheckPoint.TASK_TERMINAL)
            elif task.state is TaskState.COMPLETED and self._pending_end is not None:
                reason, self._acceptance = self._pending_end
                self.arbiter.signal(reason, point=CheckPoint.CRITIC_DECISION)
        self.arbiter.check(CheckPoint.TASK_TERMINAL)

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
        pairs = assign_hypothesis_ids(result.output.hypotheses, self.board, ledger, self.board.next_hypothesis_id)
        await self.board.add_hypothesis_revisions(outcome.task_id, pairs)
        await self.board.set_claims(apply_claim_updates(prior, result.output.claim_updates))

    def _completed_request_tasks(self) -> list[tuple[str, EvidenceRequest]]:
        pairs = []
        for task in self.graph:
            if task.kind is TaskKind.EVIDENCE_REQUEST and task.state is TaskState.COMPLETED and task.evidence_request_id:
                request = self.board.evidence_request(task.evidence_request_id)
                if request is not None:
                    pairs.append((task.task_id, request))
        return pairs

    def _commit_critic(self, task: Task, outcome: AgentOutcome) -> None:
        output: CriticOutput = outcome.output  # type: ignore[assignment]
        # Raises StructuredOutputFailure for unparseable parameters, which fails this attempt.
        action = self.critic_loop.on_critic_completed(task, output, self._completed_request_tasks())
        for request in action.requests:
            self.board.put_evidence_request(request)
        self.trace.record(
            "critic_decision",
            critic_round=action.critic_round,
            accepted_hypothesis_id=action.acceptance,
            evidence_requests=action.requests,
        )
        for duplicate in action.duplicates:
            self.trace.record(
                "duplicate_discarded",
                request=duplicate.request,
                matched_task_id=duplicate.matched_task_id,
                matched_request_id=duplicate.matched_request_id,
            )
        for new_task in action.tasks:
            # Each add pumps, so the round's Specialist Tasks start as soon as slots allow; any left
            # start when this Critic Task's terminal transition frees its slot (Req 7.1, 7.4).
            self.scheduler.add(new_task)
        if action.end is not None:
            self._pending_end = (action.end, action.acceptance)

    def _trace_violations(self, violations) -> None:
        for violation in violations:
            self.trace.record("citation_violation", **violation.trace_fields())

    # termination and loop end

    def _on_loop_end(self, reason: TerminationReason) -> None:
        """Runs synchronously when the reason is recorded: stop everything but Reporting (Req 18.8)."""
        self._tick()
        self.trace.record("termination", reason=reason, usage=self.board.usage)
        self.layer.close()
        self.gateway.end_loop()
        self.scheduler.cancel_all(include_reporting=False)

    async def _loop_end_steps(self) -> None:
        """Design loop-end steps 2-5, each at most once, with no await on a Tool or an LLM."""
        reason = self.arbiter.reason
        assert reason is not None
        # Step 1 ran when the reason was recorded; repeat it for Tasks added after that (a plan whose
        # own LLM call ended the loop). Cancelling is idempotent.
        self.scheduler.cancel_all(include_reporting=False)
        if not self._gaps_recorded:
            self._gaps_recorded = True
            await self._record_unfulfilled_requests(reason)
        if self.board.termination is None:
            self._tick()
            await self.board.set_termination(reason)
        if not self._confidence_applied:
            self._confidence_applied = True
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
        if self.board.verdict is None:
            decision = investigator_verdict(
                reason,
                self._acceptance,
                ranked_hypotheses(self.board),
                self.board.ledger_view(),
                self.config.policy.conclusive_threshold,
            )
            await self.board.set_verdict(decision.verdict, self._acceptance)
            self.trace.record("verdict_set", verdict=decision.verdict, conditions=decision.conditions)

    async def _record_unfulfilled_requests(self, reason: TerminationReason) -> None:
        """One Data_Gap per kept Evidence_Request without a completed or failed Task (Req 17.3)."""
        for request in self.board.evidence_requests():
            if request.status is EvidenceRequestStatus.DUPLICATE:
                continue
            if request.task_id is not None and request.task_id in self.graph:
                if self.graph.get(request.task_id).state in (TaskState.COMPLETED, TaskState.FAILED):
                    continue
            self.board.put_evidence_request(request.model_copy(update={"status": EvidenceRequestStatus.UNFULFILLED}))
            await self.board.add_gap(
                DataGapDraft(
                    source=request.target,
                    query=request.suggested_params,
                    reason=f"unfulfilled_evidence_request:{request.request_id}:{reason}",
                    affected_time_range=input_time_range(request.suggested_params),
                )
            )

    def _reporting_task(self) -> Task | None:
        return next((t for t in self.graph if t.kind is TaskKind.REPORTING), None)

    def _drg(self) -> InvestigationReport:
        self._tick()
        return DeterministicReportGenerator(ReportAssembler()).build(
            self.board, SystemName.MULTI, self.limits, self.config.report.max_recommended_actions
        )

    async def _report_path(self) -> InvestigationReport:
        reporting = self._reporting_task()
        if reporting is None or reporting.state is not TaskState.PENDING:
            return self._drg()
        remaining = self.limits.tokens - self.board.usage.total_tokens
        if self.arbiter.reason is TerminationReason.WALL_CLOCK_TIMEOUT or remaining < self.limits.reporting_reserve:
            self.scheduler.cancel_pending(reporting.task_id)  # before any Reporting LLM call (Req 12.7)
            return self._drg()
        self.scheduler.release_reporting()
        await self.scheduler.wait_terminal(reporting.task_id)
        self._tick()
        if self._reporting_outcome is None:
            # Failed after its retry (Data_Gap recorded with after_verdict, Req 12.6, 17.10) or cancelled.
            return self._drg()
        return self._reporting_report(self._reporting_outcome)

    def _reporting_report(self, outcome: AgentOutcome) -> InvestigationReport:
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
        return ReportAssembler().assemble(
            self.board,
            system=SystemName.MULTI,
            built_by="reporting_agent",
            actions=actions,
            limits=self.limits,
            extra_claims=extra,
        )

    # the run

    async def _investigate(self) -> InvestigationReport:
        graph, _ = await self.planner.plan(self.inputs.incident_report, self.factory)
        self.scheduler.extend(list(graph))
        await self.arbiter.wait()
        await self._loop_end_steps()
        return await self._report_path()

    async def _watchdog(self) -> None:
        """Return when the wall-clock timeout elapses, measured on the Clock from Run start.

        Expressed as a Clock timeout on an event that never fires, so it is a timer on both clocks
        (an event-loop timer on the SystemClock) and is cancelled cleanly when the Run finishes."""
        never = asyncio.Event()
        with suppress(ClockTimeout):
            await self.clock.wait_for(never.wait(), self.limits.wall_clock_s)

    async def _after_timeout(self) -> InvestigationReport:
        """The watchdog fired: no further Tool_Call or LLM call, every Task cancelled, then the DRG (Req 18.5, 18.6)."""
        self.layer.close()
        self.gateway.close()
        self.arbiter.signal(TerminationReason.WALL_CLOCK_TIMEOUT, point=CheckPoint.WATCHDOG)
        self.scheduler.cancel_all(include_reporting=True)
        await self._loop_end_steps()
        return self._drg()

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
        await self.board.record_initial_claims()  # before the first Task (Req 1.4)

        watchdog = asyncio.ensure_future(self._watchdog())
        body = asyncio.ensure_future(self._investigate())
        try:
            await asyncio.wait({body, watchdog}, return_when=asyncio.FIRST_COMPLETED)
            if body.done():
                report = body.result()
            else:
                body.cancel()
                with suppress(asyncio.CancelledError):
                    await body
                report = await self._after_timeout()
        finally:
            for future in (watchdog, body):
                if not future.done():
                    future.cancel()
                    with suppress(asyncio.CancelledError):
                        await future
            await self.scheduler.shutdown()

        self._tick()
        usage = self.board.usage
        self.trace.record(
            "run_ended",
            termination_reason=report.termination_reason,
            critic_rounds={"used": usage.critic_rounds, "limit": self.limits.critic_rounds},
            tool_calls={"used": usage.tool_calls, "limit": self.limits.tool_calls},
            input_tokens=usage.tokens.input_tokens,
            output_tokens=usage.tokens.output_tokens,
            tokens={"used": usage.total_tokens, "limit": self.limits.tokens},
            wall_clock_s={"used": usage.elapsed_s, "limit": self.limits.wall_clock_s},
        )
        trace = self.trace.to_trace()
        ledger = self.board.to_state()
        self.sink.write(report, trace, ledger)
        return RunResult(report=report, trace=trace, ledger=ledger, elapsed_s=usage.elapsed_s)


async def run_investigator(inputs: RunInputs, deps: RunDependencies, sink: RunOutputSink) -> RunResult:
    return await InvestigatorRun(inputs, deps, sink).execute()
