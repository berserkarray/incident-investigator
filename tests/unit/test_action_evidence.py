"""Req 12.4 ordering and citation-ID clarity.

Absent IDs are what the Citation_Validator strips, so a mitigation, fix or prevention action is
checked on the IDs it keeps: it must cite at least one Evidence_ID in the ledger. Before the fix, an
action citing only non-evidence IDs (a finding ID such as F001) passed validation and reached the
report with no evidence IDs.
"""

import json

from factories import at, make_config, make_incident_report
from harness import accept_rank1, context_of, generated_incident, reasoning_reply, run_scripted

from incident_investigator.agents.prompts import templates
from incident_investigator.agents.reporting import check_reporting
from incident_investigator.baseline.prompt import baseline_prompt
from incident_investigator.baseline.react import check_baseline_step
from incident_investigator.llm.base import ValidationContext
from incident_investigator.llm.fake import ScriptEntry
from incident_investigator.models.evidence import LedgerEntry, LedgerView
from incident_investigator.models.hypothesis import HypothesisRevision
from incident_investigator.models.outputs import BaselineStep, ReportingOutput
from incident_investigator.models.vocab import AgentName, CallKind, Reliability, ToolName

LEDGER = LedgerView(items={"E0001": LedgerEntry(tool=ToolName.QUERY_LOGS, reliability=Reliability.COMPLETE)})
NOT_EVIDENCE = "none of which is an evidence ID in the ledger"


def action(category: str, *ids: str, hypothesis_id: str = "H1") -> dict:
    return {"text": f"{category} step", "category": category, "hypothesis_id": hypothesis_id, "evidence_ids": list(ids)}


H1 = HypothesisRevision(
    hypothesis_id="H1", revision=1, task_id="T3", recorded_at=at(0), component="checkout-a1b",
    cause_type="config_error", statement="s", confidence=0.8, rank=1, evidence_sufficient=True,
)


def reporting_problems(*actions: dict) -> list[str]:
    output = ReportingOutput.model_validate({"recommended_actions": list(actions)})
    return check_reporting(output, ValidationContext(ledger=LEDGER, latest_hypotheses=(H1,), max_actions=6))


def test_an_action_citing_only_non_evidence_ids_is_rejected():
    for category in ("mitigation", "fix", "prevention"):
        (problem,) = reporting_problems(action(category, "F001", "G002"))
        assert problem.startswith("recommended_actions.0.evidence_ids") and "F001, G002" in problem and NOT_EVIDENCE in problem


def test_an_action_keeping_one_ledger_id_passes():
    assert reporting_problems(action("mitigation", "F001", "E0001")) == []
    assert reporting_problems(action("further_investigation")) == []  # no evidence needed (Req 12.4)
    assert reporting_problems(action("fix", "E0404")) != []  # an E-style ID absent from the ledger is stripped too


def test_baseline_final_answer_actions_follow_the_same_rule():
    def problems(*actions: dict) -> list[str]:
        step = BaselineStep.model_validate(
            {
                "final_answer": {
                    "outcome": "inconclusive",
                    "hypotheses": [
                        {"hypothesis_id": "H1", "component": "checkout-a1b", "cause_type": "config_error", "statement": "s",
                         "confidence": 0.5, "rank": 1, "evidence_sufficient": False}
                    ],
                    "recommended_actions": list(actions),
                }
            }
        )
        vctx = ValidationContext(ledger=LEDGER, valid_components=frozenset({"checkout-a1b"}), max_actions=6)
        return check_baseline_step(step, vctx)

    (problem,) = problems(action("mitigation", "F003"))
    assert problem.startswith("final_answer.recommended_actions.0.evidence_ids") and NOT_EVIDENCE in problem
    assert problems(action("mitigation", "E0001")) == []


def _reporting(cite_evidence: bool):
    """A Reporting reply whose mitigation cites a finding ID, plus a real Evidence_ID when ``cite_evidence``."""

    def respond(request) -> str:
        ctx = context_of(request)
        rank1 = min(ctx["hypotheses"], key=lambda h: h["rank"])
        finding = ctx["findings"][0]["finding_id"]
        ids = [finding, ctx["evidence"][0]["evidence_id"]] if cite_evidence else [finding]
        return json.dumps({"recommended_actions": [action("mitigation", *ids, hypothesis_id=rank1["hypothesis_id"])]})

    return respond


async def test_reporting_repairs_an_action_backed_only_by_a_finding_id(tmp_path):
    incident = generated_incident(tmp_path / "data")
    script = [
        ScriptEntry(AgentName.REASONING, respond=reasoning_reply()),
        ScriptEntry(AgentName.CRITIC, respond=accept_rank1),
        ScriptEntry(AgentName.REPORTING, respond=_reporting(cite_evidence=False)),
        ScriptEntry(AgentName.REPORTING, respond=_reporting(cite_evidence=True)),  # the repair
    ]
    outcome = await run_scripted("multi", incident, tmp_path / "out", script)

    reporting = outcome.prompts_by(AgentName.REPORTING)
    assert [p.call_kind for p in reporting] == [CallKind.REPORTING, CallKind.REPAIR]
    assert NOT_EVIDENCE in reporting[1].user
    report = outcome.report
    assert report.built_by == "reporting_agent"
    (mitigation,) = [a for a in report.recommended_actions if a.category == "mitigation"]
    assert len(mitigation.evidence_ids) == 1 and mitigation.evidence_ids[0].startswith("E")
    removed = [e.removed_evidence_id for e in outcome.events("citation_violation") if e.target_kind == "recommended_action"]
    assert len(removed) == 1 and removed[0].startswith("F")  # stripped first; the Evidence_ID is kept


async def test_reporting_that_never_cites_evidence_falls_back_without_unsupported_actions(tmp_path):
    incident = generated_incident(tmp_path / "data")
    script = [
        ScriptEntry(AgentName.REASONING, respond=reasoning_reply()),
        ScriptEntry(AgentName.CRITIC, respond=accept_rank1),
        *[ScriptEntry(AgentName.REPORTING, respond=_reporting(cite_evidence=False)) for _ in range(4)],
    ]
    outcome = await run_scripted("multi", incident, tmp_path / "out", script)

    assert outcome.report.built_by == "deterministic"
    for item in outcome.report.recommended_actions:
        assert item.category == "further_investigation" or item.evidence_ids, item


# citation-ID clarity in prompts


def test_every_prompt_template_says_only_evidence_ids_are_evidence():
    for name, template in templates.ALL_TEMPLATES.items():
        assert templates.CITATION_IDS in template, name


async def test_board_prompts_label_finding_and_gap_ids_distinctly(tmp_path):
    incident = generated_incident(tmp_path / "data", "logs-timeout")  # query_logs always times out: a Data_Gap
    outcome = await run_scripted("multi", incident, tmp_path / "out")
    for caller in (AgentName.REASONING, AgentName.CRITIC, AgentName.REPORTING):
        ctx = context_of(outcome.prompts_by(caller)[0])
        assert set(ctx["id_types"]) == {"evidence_id", "finding_id", "data_gap_id", "claim_id", "hypothesis_id", "request_id"}
        assert ctx["findings"] and all(f["finding_id"].startswith("F") and "claim_id" not in f for f in ctx["findings"])
        assert all(e.startswith("E") for f in ctx["findings"] for e in f["cites_evidence_ids"])
        assert ctx["data_gaps"] and all(g["data_gap_id"].startswith("G") and "gap_id" not in g for g in ctx["data_gaps"])


def test_baseline_prompt_asks_for_the_missing_evidence_before_inconclusive():
    rule = templates.BASELINE_QUERY_BEFORE_INCONCLUSIVE
    assert rule in templates.BASELINE
    assert rule not in templates.INVESTIGATION_CHECKLIST
    assert all(rule not in t for name, t in templates.ALL_TEMPLATES.items() if name != "baseline")

    config = make_config(budgets={"tool_calls": 60}).for_system_under_test()
    common = dict(incident=make_incident_report(), claims=(), components=["checkout-a1b"], history=[], config=config, step=1)
    assert "56 tool calls remain" in baseline_prompt(**common, tool_calls_used=4)
    assert "tool-call budget is spent" in baseline_prompt(**common, tool_calls_used=60)
