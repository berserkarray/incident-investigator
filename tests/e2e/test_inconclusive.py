"""The inconclusive path end to end (task 9.4; Req 16.4, 17.4, 17.6, 19.3, 19.5, 30.7)."""

import pytest
from factories import make_incident_report, make_snapshot
from harness import (
    accept_rank1,
    distinct_gap_count,
    invalid,
    reasoning_reply,
    run_scripted,
    write_incident_dir,
)

from incident_investigator.llm.fake import ScriptEntry
from incident_investigator.models.snapshot import FailureProfile, ForcedFailure
from incident_investigator.models.vocab import AgentName, FailureType, TaskState, TerminationReason, ToolName, Verdict
from incident_investigator.report.writer import REPORT_MD

PENALTY = 0.1


@pytest.fixture
def incident(tmp_path):
    return write_incident_dir(tmp_path / "data", make_snapshot(), make_incident_report())


def section(markdown: str, heading: str) -> str:
    """The body of one ``## heading`` section of a Markdown report."""
    body = markdown.split(f"## {heading}\n", 1)[1]
    return body.split("\n## ", 1)[0]


def assert_penalty_applied(report, penalty: float) -> int:
    """Req 17.4, 17.9: adjusted confidence = max(0, raw - penalty * distinct Data_Gaps) for every Hypothesis."""
    n = distinct_gap_count(report)
    for h in (*report.hypotheses, *report.superseded_hypotheses):
        assert h.confidence == pytest.approx(max(0.0, h.raw_confidence - penalty * n)), h.hypothesis_id
    return n


async def test_run_without_hypotheses_is_inconclusive_with_missing_evidence(incident, tmp_path):
    outcome = await run_scripted("multi", incident, tmp_path / "out", invalid(AgentName.REASONING, 4))
    report = outcome.report

    assert report.termination_reason is TerminationReason.NO_HYPOTHESES
    assert report.verdict is Verdict.INCONCLUSIVE
    assert report.hypotheses == () and report.recommended_actions == ()  # D7: no Hypotheses, no actions
    (reasoning,) = [t for t in outcome.ledger.tasks if t.agent is AgentName.REASONING]
    assert reasoning.state is TaskState.FAILED
    # Req 19.5: the missing-evidence list carries every Data_Gap, labelled by source
    gap = next(g for g in report.data_gaps if g.task_id == reasoning.task_id)
    assert any(m.source == "data_gap" and m.ref == gap.gap_id for m in report.missing_evidence)

    markdown = outcome.files()[REPORT_MD]
    assert section(markdown, "Hypotheses").strip() == "_No entries._"  # Req 19.3: zero Hypotheses listed
    missing = section(markdown, "Missing Evidence")
    assert "[data_gap]" in missing and gap.reason in missing
    assert "**inconclusive**" in section(markdown, "Verdict")


async def test_agent_failure_gap_lowers_confidence_by_the_penalty(incident, tmp_path):
    """A failed Specialist Task leaves a Data_Gap; every Hypothesis loses one penalty per distinct gap."""
    script = [
        *invalid(AgentName.OBSERVABILITY, 4),  # both attempts fail after their repair
        ScriptEntry(AgentName.REASONING, respond=reasoning_reply(confidence=0.9)),
        ScriptEntry(AgentName.CRITIC, respond=accept_rank1),
    ]
    outcome = await run_scripted(
        "multi", incident, tmp_path / "out", script, policy={"data_gap_penalty": PENALTY}
    )
    report = outcome.report

    (observability,) = [t for t in outcome.ledger.tasks if t.agent is AgentName.OBSERVABILITY]
    assert observability.state is TaskState.FAILED
    assert any(g.task_id == observability.task_id and str(g.source) == "observability" for g in report.data_gaps)
    assert assert_penalty_applied(report, PENALTY) >= 1
    rank1 = report.hypotheses[0]
    assert rank1.confidence < rank1.raw_confidence
    # The penalty is applied once, before the Verdict, and the trace records it (Req 19.6)
    (applied,) = outcome.events("confidence_applied")
    assert applied.penalty == PENALTY and applied.gap_count == distinct_gap_count(report)
    types = [e.type for e in outcome.trace.events]
    assert types.index("confidence_applied") < types.index("verdict_set")
    markdown = outcome.files()[REPORT_MD]
    assert f"Confidence: {rank1.confidence:.2f} (before data-gap penalty: {rank1.raw_confidence:.2f})" in markdown


async def test_forced_tool_failure_is_listed_and_lowers_confidence(tmp_path):
    """Forced query_logs timeouts on every attempt: one Data_Gap (query_logs, timeout), listed and penalised."""
    snapshot = make_snapshot(failure_profile=FailureProfile(forced=(ForcedFailure(tool=ToolName.QUERY_LOGS, failure_type=FailureType.TIMEOUT),)))
    incident = write_incident_dir(tmp_path / "data", snapshot, make_incident_report())
    script = [ScriptEntry(AgentName.REASONING, respond=reasoning_reply(confidence=0.9))]
    outcome = await run_scripted("multi", incident, tmp_path / "out", script, policy={"data_gap_penalty": PENALTY})
    report = outcome.report

    timeouts = [g for g in report.data_gaps if g.source is ToolName.QUERY_LOGS and g.reason == "timeout"]
    assert timeouts, report.data_gaps
    attempts = [e for e in outcome.events("tool_call") if e.tool is ToolName.QUERY_LOGS]
    assert attempts and all(e.error_code == "timeout" for e in attempts)
    assert assert_penalty_applied(report, PENALTY) >= 1
    markdown = section(outcome.files()[REPORT_MD], "Data Gaps")
    assert all(g.gap_id in markdown for g in timeouts) and "query_logs" in markdown and "timeout" in markdown
