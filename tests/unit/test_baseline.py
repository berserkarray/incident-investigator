"""Baseline_Agent ReAct loop: final answer, step failure, budget and timeout stops (Req 25)."""

import json
from pathlib import Path

from factories import make_config, make_incident_report, window, write_snapshot_dir

from incident_investigator.baseline.react import BaselineRun, check_baseline_step
from incident_investigator.clock import FakeClock
from incident_investigator.llm.base import ValidationContext
from incident_investigator.llm.fake import ScriptedFakeLLM, ScriptEntry
from incident_investigator.models.budget import TokenCount
from incident_investigator.models.outputs import BaselineStep
from incident_investigator.models.run import RunInputs
from incident_investigator.models.vocab import CallKind, ClaimStatus, SystemName, TerminationReason, Verdict
from incident_investigator.orchestrator.run import InvestigatorRun
from incident_investigator.report.writer import MemoryRunOutputSink
from incident_investigator.runtime import RunDependencies

LOGS_STEP = {
    "reasoning_note": "Check the error logs first.",
    "tool_request": {
        "tool": "query_logs",
        "params": {"service": "checkout-a1b", "time_range": window(0, 120).model_dump(mode="json"), "levels": ["error"], "limit": 25},
    },
}


def final_step(outcome="conclusive", support=("E0001",), statuses=None) -> dict:
    statuses = statuses or [
        {"claim_id": "IC1", "status": "verified", "corroborating_ids": ["E0001"]},
        {"claim_id": "IC2", "status": "unverified"},
        {"claim_id": "IC3", "status": "unverified"},
    ]
    return {
        "reasoning_note": "Enough evidence.",
        "final_answer": {
            "outcome": outcome,
            "hypotheses": [
                {
                    "hypothesis_id": "H1",
                    "component": "checkout-a1b",
                    "cause_type": "config_error",
                    "statement": "A bad configuration change on checkout.",
                    "supporting_evidence_ids": list(support),
                    "confidence": 0.9,
                    "rank": 1,
                    "evidence_sufficient": True,
                },
                {
                    "hypothesis_id": "H2",
                    "component": "orders-db-456",
                    "cause_type": "database_failover",
                    "statement": "A database failover.",
                    "confidence": 0.3,
                    "rank": 2,
                    "evidence_sufficient": False,
                },
            ],
            "claim_statuses": statuses,
            "claims": [{"text": "checkout logged a 504 at 01:01", "evidence_ids": ["E0001"]}],
            "recommended_actions": [
                {"text": "Roll back the config change.", "category": "mitigation", "hypothesis_id": "H1", "evidence_ids": ["E0001"]}
            ],
        },
    }


def entry(step: dict | str, **kwargs) -> ScriptEntry:
    text = step if isinstance(step, str) else json.dumps(step)
    return ScriptEntry("baseline", kwargs.pop("call_kind", None), text=text, **kwargs)


def setup_run(tmp_path: Path, entries, **sections):
    snapshot_dir = write_snapshot_dir(tmp_path / "snapshots")
    config = make_config(
        paths={"snapshot_root": str(tmp_path / "snapshots"), "answer_key_root": str(tmp_path / "keys")}, **sections
    )
    clock = FakeClock()
    fake = ScriptedFakeLLM(entries, clock)
    sink = MemoryRunOutputSink()
    inputs = RunInputs(snapshot_dir=snapshot_dir, incident_report=make_incident_report(), config=config.for_system_under_test())
    return BaselineRun(inputs, RunDependencies(llm_client=fake, clock=clock), sink), fake, sink


async def test_final_answer_is_validated_and_assembled(tmp_path):
    run, fake, sink = setup_run(tmp_path, [entry(LOGS_STEP), entry(final_step(support=("E0001", "E0999")))])
    result = await run.execute()
    report, trace = result.report, result.trace

    assert len(sink.outputs) == 1  # exactly one report
    assert report.system is SystemName.BASELINE and report.built_by == "baseline"
    assert report.termination_reason is TerminationReason.FINAL_ANSWER
    assert report.verdict is Verdict.CONCLUSIVE
    assert report.hypotheses[0].supporting_evidence_ids == ("E0001",)  # the absent E0999 was removed
    assert [v.removed_evidence_id for v in trace.of_type("citation_violation")] == ["E0999"]
    assert [f.evidence_ids for f in report.facts] == [("E0001",)]
    assert report.claim_statuses[0].status is ClaimStatus.VERIFIED
    assert [a.producer for a in report.recommended_actions] == ["baseline"]
    assert report.budget_usage.critic_rounds.used == 0  # zero Critic_Rounds (Req 25.2)

    # the Observation of step 1 (Evidence_ID and records) reaches step 2 (Req 25.1, 25.10)
    assert [p.call_kind for p in fake.prompts] == [CallKind.BASELINE_STEP, CallKind.BASELINE_STEP]
    assert '"evidence_id":"E0001"' in fake.prompts[1].user and "timeout 504" in fake.prompts[1].user
    assert [(e.step, e.action) for e in trace.of_type("baseline_step")] == [(1, "tool"), (2, "final")]
    assert [e.reason for e in trace.of_type("termination")] == [TerminationReason.FINAL_ANSWER]
    assert trace.of_type("run_ended")[0].termination_reason is TerminationReason.FINAL_ANSWER


async def test_claims_start_unverified_before_the_first_step(tmp_path):
    run, fake, _ = setup_run(tmp_path, [entry(final_step(outcome="inconclusive", support=()))])
    result = await run.execute()
    first_context = fake.prompts[0].user
    assert '"status":"unverified"' in first_context and '"label":"assumption"' in first_context
    assert result.report.verdict is Verdict.INCONCLUSIVE  # declared inconclusive (Req 25.5)


async def test_step_failing_twice_ends_with_baseline_failed(tmp_path):
    missing_claims = final_step(statuses=[{"claim_id": "IC1", "status": "unverified"}])
    run, fake, sink = setup_run(tmp_path, [entry(missing_claims) for _ in range(4)])  # step + repair, twice
    result = await run.execute()
    report = result.report
    assert report.termination_reason is TerminationReason.BASELINE_FAILED
    assert report.built_by == "deterministic" and report.verdict is Verdict.INCONCLUSIVE
    (gap,) = report.data_gaps
    assert (gap.source, gap.step, gap.reason) == ("baseline", 1, "structured_output")
    failures = result.trace.of_type("agent_failure")
    assert [(f.agent, f.step, f.attempt) for f in failures] == [("baseline", 1, 1), ("baseline", 1, 2)]
    assert "missing ['IC2', 'IC3']" in fake.prompts[1].user  # the repair prompt names the semantic error
    assert len(sink.outputs) == 1 and fake.remaining() == 0


async def test_tool_call_budget_stops_before_the_next_step(tmp_path):
    run, fake, _ = setup_run(tmp_path, [entry(LOGS_STEP), entry(LOGS_STEP)], budgets={"tool_calls": 1})
    result = await run.execute()
    assert result.report.termination_reason is TerminationReason.TOOL_CALL_BUDGET_EXHAUSTED
    assert result.report.budget_limit.limit == "tool_calls"
    assert result.report.verdict is Verdict.INCONCLUSIVE and result.report.built_by == "deterministic"
    assert len(fake.prompts) == 1 and fake.remaining() == 1


async def test_token_budget_uses_the_full_budget_without_a_reporting_reserve(tmp_path):
    usage = TokenCount(input_tokens=600)
    entries = [entry(LOGS_STEP, usage=usage), entry(LOGS_STEP, usage=usage), entry(LOGS_STEP, usage=usage)]
    run, fake, _ = setup_run(tmp_path, entries, budgets={"tokens": 1000, "reporting_reserve_tokens": 500})
    result = await run.execute()
    assert result.report.termination_reason is TerminationReason.TOKEN_BUDGET_EXHAUSTED
    assert len(fake.prompts) == 2  # 600 tokens is past tokens - reserve, but the baseline has no reserve


async def test_wall_clock_timeout_builds_the_deterministic_report(tmp_path):
    entries = [entry(LOGS_STEP, delay_s=50), entry(LOGS_STEP)]
    run, fake, sink = setup_run(tmp_path, entries, budgets={"wall_clock_timeout_s": 10})
    result = await run.execute()
    assert result.report.termination_reason is TerminationReason.WALL_CLOCK_TIMEOUT
    assert result.report.budget_limit.limit == "wall_clock_s"
    assert len(fake.prompts) == 1 and fake.remaining() == 1  # no LLM call after the timeout
    assert len(sink.outputs) == 1


async def test_per_step_timeout_is_an_agent_failure(tmp_path):
    entries = [entry(LOGS_STEP, delay_s=50), entry(LOGS_STEP, delay_s=50)]
    run, _, _ = setup_run(tmp_path, entries, orchestrator={"per_task_timeout_s": 5})
    result = await run.execute()
    assert result.report.termination_reason is TerminationReason.BASELINE_FAILED
    assert [g.reason for g in result.report.data_gaps] == ["per_task_timeout"]


def test_final_answer_check_rules():
    vctx = ValidationContext(valid_components=frozenset({"checkout-a1b"}), incident_claim_ids=("IC1",), max_actions=1)
    step = BaselineStep.model_validate(
        {
            "final_answer": {
                "outcome": "conclusive",
                "hypotheses": [
                    {"hypothesis_id": "H1", "component": "nope", "cause_type": "config_error", "statement": "s",
                     "confidence": 0.5, "rank": 1, "evidence_sufficient": False}
                ],
                "claim_statuses": [{"claim_id": "IC1", "status": "verified"}],
                "recommended_actions": [
                    {"text": "a", "category": "further_investigation", "hypothesis_id": "H7"},
                    {"text": "b", "category": "further_investigation", "hypothesis_id": "H1"},
                ],
            }
        }
    )
    problems = " ".join(check_baseline_step(step, vctx))
    for fragment in ("'nope' is not in context.components", "verified claim cites", "'H7' is not a hypothesis", "at most 1 actions"):
        assert fragment in problems
    assert check_baseline_step(BaselineStep.model_validate(LOGS_STEP), vctx) == []


def _layer_shape(layer) -> dict:
    shape = {name: type(value).__name__ for name, value in vars(layer).items()}
    shape["budget_limit"] = layer._budget.limit
    shape["summary_size"] = layer._summarizer.size
    return shape


def test_baseline_tool_layer_matches_the_investigator(tmp_path):
    """Req 25.2: same Tool_Layer stack and settings. Fails if one system gets a different injector,
    breaker, retry policy, budget or summariser than the other."""
    baseline, fake, sink = setup_run(tmp_path, [])
    investigator = InvestigatorRun(baseline.inputs, baseline.deps, sink)
    baseline._setup()
    investigator._setup()
    assert _layer_shape(baseline.layer) == _layer_shape(investigator.layer)
    assert baseline.gateway._limits.tokens == investigator.gateway._limits.tokens
