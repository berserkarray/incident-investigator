"""Baseline_Agent: one sequential ReAct loop over the same Tools, budgets and report model (Req 25).

Each step is one ``baseline_step`` LLM call (plus at most one repair) that yields a reasoning note
and exactly one Tool_Request or a final answer. Observations, including error Observations, go back
into the next step's prompt. A final answer goes through the Citation_Validator, the
Confidence_Policy and the Baseline Verdict rule and is assembled into the shared
Investigation_Report. A budget or timeout stop, or a step that fails twice, ends with a report from
the Deterministic_Report_Generator.

The Baseline_Agent keeps its own Blackboard as its Run_Ledger and runs no other Agent. It has no
Reporting_Agent, so the Reporting_Agent reserve does not apply: its LLM gate stops at the full
token Budget (Req 25.1, 25.6).
"""

from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import BaseModel

from ..agents.prompts import templates
from ..clock import ClockTimeout
from ..errors import BudgetBlocked, RunClosed
from ..llm.base import ValidationContext
from ..llm.gateway import LLMGateway
from ..llm.validator import StructuredOutputFailure
from ..models.claims import LabelledClaim
from ..models.gaps import DataGapDraft
from ..models.outputs import BaselineFinalAnswer, BaselineStep
from ..models.report import InvestigationReport, RecommendedAction
from ..models.run import RunInputs, RunResult
from ..models.vocab import (
    BASELINE,
    CallKind,
    ClaimStatus,
    FailureKind,
    SystemName,
    TerminationReason,
    ToolName,
    Verdict,
)
from ..report.assembler import ReportAssembler, ranked_hypotheses
from ..report.deterministic import DeterministicReportGenerator
from ..report.writer import RunOutputSink
from ..runtime import RunDependencies
from ..state.blackboard import Blackboard
from ..state.citations import CitableOutput, CitationValidator, apply_claim_updates
from ..state.confidence import apply_confidence_policy
from ..state.verdict import baseline_verdict
from ..tools.layer import BoardToolBudget, ScopedToolPort, ToolLayer
from ..tools.snapshot import PathGuard, SnapshotLoader
from ..tools.summary import ObservationSummarizer
from ..tracing import TraceRecorder
from .prompt import StepRecord, baseline_prompt

ALL_TOOLS: frozenset[ToolName] = frozenset(ToolName)


def check_baseline_step(output: BaseModel, vctx: ValidationContext) -> list[str]:
    """Context rules for a final answer (Req 25.7): Components, claim coverage, citations, actions."""
    assert isinstance(output, BaselineStep)
    final = output.final_answer
    if final is None:
        return []  # Tool input problems come back as invalid_input Observations, not repairs
    problems = [
        f"final_answer.hypotheses.{i}.component: {h.component!r} is not in context.components"
        for i, h in enumerate(final.hypotheses)
        if h.component not in vctx.valid_components
    ]
    given = [u.claim_id for u in final.claim_statuses]
    expected = set(vctx.incident_claim_ids)
    unknown = sorted(set(given) - expected)
    missing = sorted(expected - set(given))
    if unknown:
        problems.append(f"final_answer.claim_statuses: unknown claim IDs {unknown}")
    if missing:
        problems.append(f"final_answer.claim_statuses: give a status for every claim; missing {missing}")
    if len(given) != len(set(given)):
        problems.append("final_answer.claim_statuses: each claim appears once")
    for i, update in enumerate(final.claim_statuses):
        if update.status is ClaimStatus.VERIFIED and not update.corroborating_ids:
            problems.append(f"final_answer.claim_statuses.{i}: a verified claim cites at least one corroborating_ids entry")
        if update.status is ClaimStatus.CONTRADICTED and not update.contradicting_ids:
            problems.append(
                f"final_answer.claim_statuses.{i}: a contradicted claim cites at least one contradicting_ids entry"
            )
    hypothesis_ids = {h.hypothesis_id for h in final.hypotheses}
    for i, action in enumerate(final.recommended_actions):
        if action.hypothesis_id not in hypothesis_ids:
            problems.append(
                f"final_answer.recommended_actions.{i}.hypothesis_id: {action.hypothesis_id!r} is not a hypothesis "
                "of this final answer"
            )
    if len(final.recommended_actions) > vctx.max_actions:
        problems.append(f"final_answer.recommended_actions: at most {vctx.max_actions} actions")
    return problems


@dataclass(frozen=True)
class _LoopEnd:
    reason: TerminationReason
    final: BaselineFinalAnswer | None = None


class _StepFailed(Exception):
    def __init__(self, kind: FailureKind, message: str):
        super().__init__(message)
        self.kind = kind


class BaselineRun:
    def __init__(self, inputs: RunInputs, deps: RunDependencies, sink: RunOutputSink):
        self.inputs = inputs
        self.deps = deps
        self.sink = sink
        self.config = inputs.config
        self.clock = deps.clock
        self.validator = CitationValidator()
        self.history: list[StepRecord] = []

    # setup: the same Tool_Layer construction and budgets as the Investigator (Req 25.2)

    def _setup(self) -> None:
        loader = SnapshotLoader(PathGuard(self.config.snapshot_root))
        self.snapshot = loader.load(self.inputs.snapshot_dir)
        self.run_id = f"{self.snapshot.incident_id}-baseline-{self.config.seed}"
        self.trace = TraceRecorder(self.run_id, SystemName.BASELINE, self.clock, self.deps.redactor)
        self.board = Blackboard(self.run_id, self.snapshot.incident_id, self.inputs.incident_report, self.clock, self.trace)
        self.limits = self.config.budgets.limits()
        self.layer = ToolLayer(
            snapshot=self.snapshot,
            ledger=self.board,
            gaps=self.board,
            budget=BoardToolBudget(self.board, self.limits.tool_calls),
            trace=self.trace,
            clock=self.clock,
            summarizer=ObservationSummarizer(self.config.report.observation_summary_size),
        )
        self.tools = ScopedToolPort(self.layer, BASELINE, ALL_TOOLS)
        # No Reporting_Agent here, so no reserve: the gate stops at the full token Budget.
        gate_limits = self.limits.model_copy(update={"reporting_reserve": 0})
        self.gateway = LLMGateway(self.deps.llm_client, self.board, self.trace, gate_limits, self.config.llm.model)

    def _tick(self) -> float:
        elapsed = self.clock.monotonic() - self._started
        self.board.set_elapsed(elapsed)
        return elapsed

    # the run

    async def execute(self) -> RunResult:
        self._started = self.clock.monotonic()
        self._setup()
        self.trace.record(
            "run_started",
            system=SystemName.BASELINE,
            incident_id=self.snapshot.incident_id,
            seed=self.config.seed,
            effective_config=self.config.model_dump(mode="json"),
        )
        await self.board.record_initial_claims()  # unverified Assumptions before the first step (Req 25.8)
        end = await self._watched_loop()
        report = await self._finish(end)
        self._tick()
        usage = self.board.usage
        self.trace.record(
            "run_ended",
            termination_reason=end.reason,
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

    async def _watched_loop(self) -> _LoopEnd:
        """The loop under the wall-clock watchdog: on timeout, close the gates and stop (Req 25.6)."""
        remaining = self.limits.wall_clock_s - (self.clock.monotonic() - self._started)
        if remaining <= 0:
            return _LoopEnd(TerminationReason.WALL_CLOCK_TIMEOUT)
        try:
            return await self.clock.wait_for(self._loop(), remaining)
        except ClockTimeout:
            return _LoopEnd(TerminationReason.WALL_CLOCK_TIMEOUT)
        finally:
            self.layer.close()
            self.gateway.close()

    def _limit_reached(self) -> TerminationReason | None:
        """Checked before every step, in the Req 18.7 listing order."""
        usage = self.board.usage
        if usage.tool_calls >= self.limits.tool_calls:
            return TerminationReason.TOOL_CALL_BUDGET_EXHAUSTED
        if usage.total_tokens >= self.limits.tokens:
            return TerminationReason.TOKEN_BUDGET_EXHAUSTED
        if self._tick() >= self.limits.wall_clock_s:
            return TerminationReason.WALL_CLOCK_TIMEOUT
        return None

    async def _loop(self) -> _LoopEnd:
        step = 0
        while True:
            reason = self._limit_reached()
            if reason is not None:
                return _LoopEnd(reason)
            step += 1
            for attempt in (1, 2):
                try:
                    result = await self._step(step, attempt)
                except BudgetBlocked:
                    return _LoopEnd(TerminationReason.TOKEN_BUDGET_EXHAUSTED)
                except RunClosed:
                    return _LoopEnd(TerminationReason.WALL_CLOCK_TIMEOUT)
                except _StepFailed as failure:
                    self.trace.record(
                        "agent_failure",
                        agent=BASELINE,
                        step=step,
                        attempt=attempt,
                        failure_kind=failure.kind,
                        message=str(failure)[:2000],
                    )
                    if attempt == 2:
                        await self.board.add_gap(DataGapDraft(source=BASELINE, step=step, reason=failure.kind.value))
                        return _LoopEnd(TerminationReason.BASELINE_FAILED)
                    continue
                if result is not None:
                    return _LoopEnd(TerminationReason.FINAL_ANSWER, result)
                break

    async def _step(self, step: int, attempt: int) -> BaselineFinalAnswer | None:
        """One step attempt under the per-Task timeout. Returns the final answer, or None after a Tool_Request."""
        try:
            return await self.clock.wait_for(self._step_body(step), self.config.orchestrator.per_task_timeout_s)
        except ClockTimeout as exc:
            raise _StepFailed(FailureKind.TASK_TIMEOUT, str(exc)) from None
        except StructuredOutputFailure as exc:
            raise _StepFailed(FailureKind.STRUCTURED_OUTPUT, str(exc)) from None
        except (BudgetBlocked, RunClosed):
            raise
        except Exception as exc:  # any other defect in the step is an Agent_Failure
            raise _StepFailed(FailureKind.EXCEPTION, f"{type(exc).__name__}: {exc}") from None

    async def _step_body(self, step: int) -> BaselineFinalAnswer | None:
        board = self.board
        vctx = ValidationContext(
            ledger=board.ledger_view(),
            valid_components=self.snapshot.component_names,
            incident_claim_ids=tuple(c.claim_id for c in board.claims()),
            max_actions=self.config.report.max_recommended_actions,
        )
        user = baseline_prompt(
            incident=self.inputs.incident_report,
            claims=board.claims(),
            components=sorted(self.snapshot.component_names),
            history=self.history,
            config=self.config,
            step=step,
            tool_calls_used=board.usage.tool_calls,
        )
        output = await self.gateway.structured(
            BASELINE, CallKind.BASELINE_STEP, templates.BASELINE, user, BaselineStep, vctx, check_baseline_step
        )
        if output.final_answer is not None:
            self.trace.record("baseline_step", step=step, action="final")
            return output.final_answer
        action = output.tool_request
        assert action is not None
        observation = await self.tools.request(None, action.tool, action.params)
        self.history.append(StepRecord(step=step, reasoning_note=output.reasoning_note, observation=observation))
        self.trace.record("baseline_step", step=step, action="tool", tool=action.tool)
        return None

    # loop end

    async def _commit_final(self, final: BaselineFinalAnswer) -> tuple[RecommendedAction, ...]:
        """Citation_Validator over the whole final answer, then commit it to the Run_Ledger (Req 25.4)."""
        board = self.board
        prior = {c.claim_id: c for c in board.claims()}
        result = self.validator.validate(
            CitableOutput(
                claims=final.claims,
                hypotheses=final.hypotheses,
                actions=final.recommended_actions,
                claim_updates=final.claim_statuses,
            ),
            board.ledger_view(),
            frozenset(h.hypothesis_id for h in final.hypotheses if h.hypothesis_id),
            prior,
            BASELINE,
        )
        for violation in result.violations:
            self.trace.record("citation_violation", **violation.trace_fields())
        await board.add_hypothesis_revisions(None, [(h.hypothesis_id, h) for h in result.output.hypotheses])
        await board.set_claims(apply_claim_updates(prior, result.output.claim_updates))
        await board.add_findings(
            [
                LabelledClaim(claim_id=board.next_finding_id(), producer=BASELINE, task_id=None, body=body, label=label)
                for body, label in zip(result.output.claims, result.labels)
            ]
        )
        return tuple(
            RecommendedAction(
                action_id=f"A{i}",
                text=a.text,
                category=a.category,
                hypothesis_id=a.hypothesis_id,
                evidence_ids=a.evidence_ids,
                producer="baseline",
            )
            for i, a in enumerate(result.output.actions, start=1)
        )

    async def _finish(self, end: _LoopEnd) -> InvestigationReport:
        actions: Sequence[RecommendedAction] = ()
        if end.final is not None:
            actions = await self._commit_final(end.final)
        self._tick()
        record = await self.board.set_termination(end.reason)
        self.trace.record("termination", reason=end.reason, usage=record.usage)

        penalty = self.config.policy.data_gap_penalty
        adjustments = apply_confidence_policy(
            self.board.active_hypotheses(), self.board.superseded_hypotheses(), self.board.data_gaps(), penalty
        )
        await self.board.set_adjustments(adjustments)
        self.trace.record(
            "confidence_applied",
            adjustments=adjustments,
            gap_count=adjustments[0].gap_count if adjustments else 0,
            penalty=penalty,
        )
        declared = end.final.outcome if end.final is not None else Verdict.INCONCLUSIVE
        decision = baseline_verdict(
            declared, ranked_hypotheses(self.board), self.board.ledger_view(), self.config.policy.conclusive_threshold
        )
        await self.board.set_verdict(decision.verdict, None)
        self.trace.record("verdict_set", verdict=decision.verdict, conditions=decision.conditions)

        self._tick()
        assembler = ReportAssembler()
        if end.final is None:  # budget, timeout or failure stop: no LLM call (Req 25.6, 25.9)
            return DeterministicReportGenerator(assembler).build(
                self.board, SystemName.BASELINE, self.limits, self.config.report.max_recommended_actions
            )
        return assembler.assemble(
            self.board, system=SystemName.BASELINE, built_by="baseline", actions=actions, limits=self.limits
        )


async def run_baseline(inputs: RunInputs, deps: RunDependencies, sink: RunOutputSink) -> RunResult:
    return await BaselineRun(inputs, deps, sink).execute()
