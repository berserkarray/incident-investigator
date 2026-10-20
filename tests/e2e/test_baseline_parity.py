"""Baseline parity: both systems start from the same opening Tool_Requests (Req 25.11).

The Investigator's Specialists issue the opening requests in their first Tasks; the Baseline_Agent's
harness issues the same requests before its first LLM step, through the same Tool_Layer and against
the same Tool_Call Budget. With the same seed the Failure_Injector gives both the same outcomes.
"""

import json
from typing import Any

import pytest
from harness import RunOutcome, generated_incident, run_scripted

from incident_investigator.agents.opening import opening_plan
from incident_investigator.agents.prompts import templates
from incident_investigator.models.tools import TOOL_INPUT_ADAPTER
from incident_investigator.models.vocab import AgentName, ErrorCode, TerminationReason

TOOL_EVENTS = ("tool_call", "tool_rejected")


def _key(event: Any) -> tuple:
    """What a Tool_Request did, independent of caller, task and timing."""
    request = (str(event.tool), json.dumps(event.input, sort_keys=True))
    if event.type == "tool_rejected":
        return (*request, "rejected", str(event.code))
    error = str(event.error_code) if event.error_code else None
    return (*request, event.attempt, error, event.partial, event.stale, event.truncated)


def _openings_by_caller(outcome: RunOutcome) -> dict[str, list[Any]]:
    """Each caller's Tool_Request events before that caller's first LLM call: its code-issued openings."""
    first_llm: dict[str, int] = {}
    for event in outcome.events("llm_call"):
        first_llm.setdefault(str(event.caller), event.seq)
    by_caller: dict[str, list[Any]] = {}
    for event in outcome.trace.events:
        if event.type in TOOL_EVENTS and event.seq < first_llm.get(str(event.caller), float("inf")):
            by_caller.setdefault(str(event.caller), []).append(event)
    return by_caller


def _planned_inputs(outcome: RunOutcome) -> list[tuple[str, str, str]]:
    """``opening_plan`` for the run's incident, parsed the way the Tool_Layer parses it."""
    plan = opening_plan(outcome.inputs.incident_report, outcome.inputs.config.orchestrator)
    return [
        (
            str(agent),
            str(tool),
            json.dumps(TOOL_INPUT_ADAPTER.validate_python({**params, "tool": tool.value}).model_dump(mode="json"), sort_keys=True),
        )
        for agent, tool, params in plan
    ]


INJECTION = {
    "off": {"failure_injection": {"random_enabled": False}},
    "on": {"failure_injection": {"random_enabled": True, "timeout_p": 0.4, "partial_p": 0.3, "stale_p": 0.2}},
}


@pytest.mark.parametrize("injection", sorted(INJECTION))
async def test_both_systems_issue_identical_opening_requests(tmp_path, injection):
    incident = generated_incident(tmp_path / "data", "bad-config-deploy", 42)
    tools = INJECTION[injection]
    multi = await run_scripted("multi", incident, tmp_path / "multi", tools=tools)
    baseline = await run_scripted("baseline", incident, tmp_path / "baseline", tools=tools)

    multi_openings = _openings_by_caller(multi)
    specialists = {str(AgentName.OBSERVABILITY), str(AgentName.DEPENDENCY)}
    multi_events = [e for caller in specialists for e in multi_openings.get(caller, [])]
    baseline_events = _openings_by_caller(baseline)["baseline"]

    # the same Tool_Requests with the same outcomes, attempt by attempt
    assert sorted(map(_key, multi_events)) == sorted(map(_key, baseline_events))

    # and they are exactly the shared opening plan: four requests, each from the Specialist it names
    planned = _planned_inputs(baseline)
    first_attempts = [e for e in multi_events if e.type == "tool_rejected" or e.attempt == 1]
    assert sorted((str(e.caller), str(e.tool), json.dumps(e.input, sort_keys=True)) for e in first_attempts) == sorted(planned)
    baseline_first = [e for e in baseline_events if e.type == "tool_rejected" or e.attempt == 1]
    assert [(str(e.tool), json.dumps(e.input, sort_keys=True)) for e in baseline_first] == [p[1:] for p in planned]
    assert all(e.input["limit"] <= 25 for e in baseline_events)

    # the openings count against the Tool_Call Budget in both systems
    assert baseline.ledger.budget.tool_calls == len(baseline.events("tool_call"))
    assert multi.ledger.budget.tool_calls == len(multi.events("tool_call"))

    # the Baseline_Agent's first prompt carries every opening Observation; no baseline_step for them
    first_prompt = baseline.prompts_by("baseline")[0].user
    assert first_prompt.count('"opening":true') == 4
    assert [e.step for e in baseline.events("baseline_step")][:1] == [1]
    if injection == "on":  # the seed injects at least one failure into the openings, identically in both
        assert any(k[3] is not None or k[4] or k[5] for k in map(_key, baseline_events) if k[2] != "rejected")


async def test_baseline_openings_stop_at_the_tool_call_budget(tmp_path):
    incident = generated_incident(tmp_path / "data", "bad-config-deploy", 42)
    outcome = await run_scripted("baseline", incident, tmp_path / "out", fallback=False, budgets={"tool_calls": 2})

    assert outcome.report.termination_reason is TerminationReason.TOOL_CALL_BUDGET_EXHAUSTED
    assert len(outcome.events("tool_call")) == 2 and outcome.ledger.budget.tool_calls == 2
    rejected = outcome.events("tool_rejected")
    assert [e.code for e in rejected] == [ErrorCode.BUDGET_EXHAUSTED, ErrorCode.BUDGET_EXHAUSTED]
    gaps = [g for g in outcome.report.data_gaps if g.reason == ErrorCode.BUDGET_EXHAUSTED.value]
    assert sorted(str(g.source) for g in gaps) == sorted(str(e.tool) for e in rejected)
    assert outcome.prompts == []  # the budget ran out in the openings, so no LLM step ran
    assert outcome.events("baseline_step") == ()


def test_the_investigation_checklist_is_shared_verbatim():
    """Req 25.12: the Baseline_Agent gets the same generic guidance as the Specialists and the Reasoning_Agent."""
    checklist = templates.INVESTIGATION_CHECKLIST
    shared = (templates.OBSERVABILITY, templates.DEPENDENCY, templates.REASONING, templates.BASELINE)
    assert all(template.count(checklist) == 1 for template in shared)
