"""Prompt builders: run-time content plus one ``<context>`` JSON block per prompt (design D5).

Each builder pairs with one Prompt_Template in ``templates.py``. The ``<context>`` block is the only
part of a prompt the OfflineResponder reads, so everything an answer depends on goes into it.

Reasoning and Critic prompts carry each Evidence_Item's ID, Tool, canonical query, reliability and
Observation_Summary, never retained records (Req 10.7, 11.15). Specialist and Baseline prompts carry
the records of their own Observations, up to each request's record limit (Req 25.10).
"""

from collections.abc import Mapping, Sequence
from typing import Any

from ...llm.context import render_context
from ...models.claims import LabelledClaim
from ...models.critic import EvidenceRequest
from ...models.evidence import EvidenceItem, LedgerView, Observation
from ...models.gaps import DataGap
from ...models.hypothesis import HypothesisRevision
from ...models.incident import IncidentClaim, IncidentReport
from ...models.report import ReportHypothesis
from ...models.tools import TOOL_INPUT_MODELS
from ...models.vocab import (
    SPECIALIST_TOOLS,
    ActionCategory,
    AgentName,
    CauseType,
    ChangeType,
    ConcernCategory,
    LogLevel,
    MetricName,
    ToolName,
)
from ...state.ports import BlackboardReader

MAX_HYPOTHESES = 5

TOOL_VOCABULARIES: dict[ToolName, dict[str, list[str]]] = {
    ToolName.QUERY_LOGS: {"levels": [v.value for v in LogLevel]},
    ToolName.QUERY_METRICS: {"metric_name": [v.value for v in MetricName]},
    ToolName.GET_DEPLOYMENTS: {"change_types": [v.value for v in ChangeType]},
    ToolName.GET_DEPENDENCIES: {},
    ToolName.SEARCH_INCIDENTS: {},
}
"""Closed vocabularies a Tool's input accepts, shown next to its schema."""

DAG_RULES = (
    "1 to max_initial_tasks observability tasks and 1 to max_initial_tasks dependency tasks, each with no dependencies",
    "exactly one reasoning task whose depends_on lists every specialist task",
    "exactly one critic task whose depends_on is the reasoning task only",
    "exactly one reporting task with no dependencies; it runs after the investigation loop ends",
    "no other tasks, no cycles, unique task_id values, and depends_on names only task_ids in the plan",
)

ID_TYPES: dict[str, str] = {
    "evidence_id": "E0001, E0002, ...: the only IDs that may be cited as evidence",
    "finding_id": "F001, F002, ...: a specialist finding; not evidence, cite its cites_evidence_ids instead",
    "data_gap_id": "G001, G002, ...: evidence that could not be obtained; never evidence",
    "claim_id": "IC1, IC2, ...: an incident report claim to give a status for; not evidence",
    "hypothesis_id": "H1, H2, ...: a hypothesis; not evidence",
    "request_id": "ER1.1, ER1.2, ...: a critic evidence request; not evidence",
}
"""Legend of every ID type in board-backed prompts, so finding and gap IDs are not cited as evidence."""

CRITIC_RULES = (
    "Accept only the rank-1 hypothesis, by its hypothesis_id, and only when no evidence request is filed.",
    "Do not accept when the rank-1 hypothesis has no supporting evidence besides search_incidents evidence; "
    "file a historical_only_support request for it instead.",
    "Do not accept when every current supporting evidence item of the rank-1 hypothesis is partial or stale; "
    "file a low_reliability_support request for it instead.",
    "suggested_tool must belong to the target agent and suggested_params must be valid input for that tool.",
)


# entries


def incident_entry(report: IncidentReport) -> dict[str, Any]:
    return report.model_dump(mode="json")


def evidence_entry(item: EvidenceItem) -> dict[str, Any]:
    """ID, Tool, canonical query, reliability and Observation_Summary of one Evidence_Item. No records."""
    return {
        "evidence_id": item.evidence_id,
        "tool": item.tool.value,
        "query": item.query.model_dump(mode="json"),
        "reliability": item.reliability.value,
        "historical": item.historical,
        "observation": item.observation.model_dump(mode="json", exclude_defaults=True),
    }


def brief_evidence_entry(item: EvidenceItem) -> dict[str, Any]:
    """ID, Tool and summary only: other agents' evidence shown to a Specialist."""
    return {
        "evidence_id": item.evidence_id,
        "tool": item.tool.value,
        "reliability": item.reliability.value,
        "summary": item.observation.model_dump(mode="json", exclude_defaults=True),
    }


def observation_entry(obs: Observation) -> dict[str, Any]:
    """An Observation as the caller received it: summary, records up to the limit, error and flags."""
    return {
        "evidence_id": obs.evidence_id,
        "tool": str(obs.tool),
        "query": obs.query.model_dump(mode="json") if obs.query is not None else obs.raw_params,
        "error": obs.error.model_dump(mode="json") if obs.error else None,
        "partial": obs.partial,
        "stale": obs.stale,
        "as_of": obs.as_of.isoformat() if obs.as_of else None,
        "truncated": obs.truncated,
        "summary": obs.summary.model_dump(mode="json", exclude_defaults=True),
        "records": [r.model_dump(mode="json") for r in obs.records],
    }


def hypothesis_entry(h: HypothesisRevision | ReportHypothesis) -> dict[str, Any]:
    return {
        "hypothesis_id": h.hypothesis_id,
        "rank": h.rank,
        "component": h.component,
        "cause_type": h.cause_type.value,
        "statement": h.statement,
        "supporting_evidence_ids": list(h.supporting_evidence_ids),
        "contradicting_evidence_ids": list(h.contradicting_evidence_ids),
        "confidence": h.confidence,
        "evidence_sufficient": h.evidence_sufficient,
        "missing_evidence": [m.model_dump(mode="json") for m in h.missing_evidence],
    }


def claim_entry(claim: IncidentClaim) -> dict[str, Any]:
    return claim.model_dump(mode="json")


def request_entry(request: EvidenceRequest) -> dict[str, Any]:
    return {
        "request_id": request.request_id,
        "critic_round": request.critic_round,
        "target": request.target.value,
        "hypothesis_id": request.hypothesis_id,
        "concern": request.concern.value,
        "question": request.question,
        "suggested_tool": request.suggested_tool.value,
        "suggested_params": request.suggested_params.model_dump(mode="json"),
        "status": request.status.value,
    }


def tool_schemas(tools: Sequence[ToolName]) -> dict[str, Any]:
    return {t.value: TOOL_INPUT_MODELS[t].model_json_schema() for t in sorted(tools)}


def tool_vocabularies(tools: Sequence[ToolName]) -> dict[str, Any]:
    return {t.value: TOOL_VOCABULARIES[t] for t in sorted(tools) if TOOL_VOCABULARIES[t]}


def support_profile(h: HypothesisRevision, ledger: LedgerView) -> dict[str, Any]:
    """How the supporting citations of one Hypothesis split by reliability (inputs of Req 11.5, 11.6, 11.16)."""
    cited = [e for e in h.supporting_evidence_ids if ledger.has(e)]
    current = [e for e in cited if not ledger.is_historical(e)]
    return {
        "hypothesis_id": h.hypothesis_id,
        "complete_current": [e for e in current if ledger.is_complete_current(e)],
        "partial_or_stale_current": [e for e in current if not ledger.is_complete_current(e)],
        "historical": [e for e in cited if ledger.is_historical(e)],
    }


def finding_entry(finding: LabelledClaim) -> dict[str, Any]:
    """A Specialist finding, keyed ``finding_id`` (F...) so it is not mistaken for an evidence or claim ID."""
    return {
        "finding_id": finding.claim_id,
        "producer": str(finding.producer),
        "task_id": finding.task_id,
        "label": finding.label.value,
        "finding": finding.body.model_dump(mode="json", exclude={"evidence_ids"}),
        "cites_evidence_ids": list(finding.body.evidence_ids),
    }


def data_gap_entry(gap: DataGap) -> dict[str, Any]:
    """A Data_Gap, keyed ``data_gap_id`` (G...): a record of missing evidence, never citable as evidence."""
    data = gap.model_dump(mode="json")
    return {"data_gap_id": data.pop("gap_id"), **data}


def _board_common(board: BlackboardReader) -> dict[str, Any]:
    return {
        "id_types": dict(ID_TYPES),
        "evidence": [evidence_entry(e) for e in board.evidence()],
        "data_gaps": [data_gap_entry(g) for g in board.data_gaps()],
        "claims": [claim_entry(c) for c in board.claims()],
        "evidence_requests": [request_entry(r) for r in board.evidence_requests()],
        "findings": [finding_entry(f) for f in board.findings()],
    }


# builders


def plan_prompt(
    incident: IncidentReport,
    components: Sequence[str],
    *,
    max_initial_tasks: int,
) -> str:
    context = {
        "role": "planner",
        "incident": incident_entry(incident),
        "components": sorted(components),
        "agents": [a.value for a in AgentName if a is not AgentName.ORCHESTRATOR],
        "specialist_tools": {agent.value: sorted(t.value for t in tools) for agent, tools in SPECIALIST_TOOLS.items()},
        "max_initial_tasks": max_initial_tasks,
        "dag_rules": list(DAG_RULES),
    }
    return "Plan the initial investigation tasks for this incident.\n\n" + render_context(context)


def specialist_prompt(
    *,
    role: str,
    incident: IncidentReport,
    components: Sequence[str],
    task: dict[str, Any],
    observations: Sequence[Observation],
    tools: Sequence[ToolName],
    steps_remaining: int,
    notes: Sequence[str] = (),
    other_evidence: Sequence[EvidenceItem] = (),
    extra_vocabularies: Mapping[str, Sequence[str]] | None = None,
    final: bool = False,
) -> str:
    context = {
        "role": role,
        "incident": incident_entry(incident),
        "components": sorted(components),
        "task": task,
        "tools": tool_schemas(tools),
        "vocabularies": {**tool_vocabularies(tools), **{k: list(v) for k, v in (extra_vocabularies or {}).items()}},
        "observations": [observation_entry(o) for o in observations],
        "step_notes": list(notes),
        "other_evidence": [brief_evidence_entry(e) for e in other_evidence],
        "steps_remaining": steps_remaining,
    }
    if final:
        instruction = (
            f"Investigate as the {role} agent. The step limit of this task is reached: return only your findings "
            "for the observations in the context, citing the evidence_id of each observation you rely on."
        )
    else:
        instruction = (
            f"Investigate as the {role} agent. Your task and your tool observations so far are in the context. "
            "Request one more tool or return your findings, citing the evidence_id of each observation you rely on."
        )
    return f"{instruction}\n\n{render_context(context)}"


def reasoning_prompt(board: BlackboardReader, incident: IncidentReport, components: Sequence[str]) -> str:
    context = {
        "role": "reasoning",
        "incident": incident_entry(incident),
        "components": sorted(components),
        "cause_types": [c.value for c in CauseType],
        "latest_hypotheses": [hypothesis_entry(h) for h in board.active_hypotheses()],
        "max_hypotheses": MAX_HYPOTHESES,
        **_board_common(board),
    }
    return (
        "Develop and rank competing root-cause hypotheses from the evidence board, revising earlier hypotheses "
        "when new evidence arrives.\n\n" + render_context(context)
    )


def critic_prompt(
    board: BlackboardReader,
    incident: IncidentReport,
    components: Sequence[str],
    *,
    critic_round: int,
    round_budget: int,
    max_requests: int,
) -> str:
    active = board.active_hypotheses()
    ledger = board.ledger_view()
    context = {
        "role": "critic",
        "incident": incident_entry(incident),
        "components": sorted(components),
        "hypotheses": [hypothesis_entry(h) for h in active],
        "top_hypothesis_support": support_profile(active[0], ledger) if active else None,
        "critic_round": critic_round,
        "critic_round_budget": round_budget,
        "max_requests": max_requests,
        "concern_categories": [c.value for c in ConcernCategory],
        "rules": list(CRITIC_RULES),
        "specialist_tools": {agent.value: sorted(t.value for t in tools) for agent, tools in SPECIALIST_TOOLS.items()},
        "tools": tool_schemas(list(ToolName)),
        **_board_common(board),
    }
    return "Review the top-ranked hypothesis and either accept it or request targeted evidence.\n\n" + render_context(
        context
    )


def reporting_prompt(
    board: BlackboardReader,
    incident: IncidentReport,
    *,
    verdict: str,
    termination_reason: str,
    ranked: Sequence[ReportHypothesis],
    superseded: Sequence[ReportHypothesis],
    missing_evidence: Sequence[dict[str, Any]],
    max_actions: int,
) -> str:
    context = {
        "role": "reporting",
        "incident": incident_entry(incident),
        "verdict": verdict,
        "termination_reason": termination_reason,
        "hypotheses": [hypothesis_entry(h) for h in ranked],
        "superseded_hypotheses": [hypothesis_entry(h) for h in superseded],
        "id_types": dict(ID_TYPES),
        "evidence": [evidence_entry(e) for e in board.evidence()],
        "findings": [finding_entry(f) for f in board.findings()],
        "data_gaps": [data_gap_entry(g) for g in board.data_gaps()],
        "missing_evidence": list(missing_evidence),
        "max_actions": max_actions,
        "action_categories": [c.value for c in ActionCategory],
    }
    return "Write the remediation recommendations for the final report.\n\n" + render_context(context)
