"""Prompt builders: run-time content plus one ``<context>`` JSON block per prompt (design D5).

Reasoning and Critic prompts carry Observation_Summaries, never full records (Req 10.7, 11.15).
"""

from collections.abc import Sequence
from typing import Any

from ...llm.context import render_context
from ...models.critic import EvidenceRequest
from ...models.evidence import EvidenceItem, Observation
from ...models.hypothesis import HypothesisRevision
from ...models.incident import IncidentClaim, IncidentReport
from ...models.report import ReportHypothesis
from ...models.tools import TOOL_INPUT_MODELS
from ...models.vocab import CauseType, ConcernCategory, SPECIALIST_TOOLS, ToolName
from ...state.ports import BlackboardReader


def incident_entry(report: IncidentReport) -> dict[str, Any]:
    return report.model_dump(mode="json")


def evidence_entry(item: EvidenceItem) -> dict[str, Any]:
    return {
        "evidence_id": item.evidence_id,
        "tool": item.tool.value,
        "query": item.query.model_dump(mode="json"),
        "reliability": item.reliability.value,
        "historical": item.historical,
        "observation": item.observation.model_dump(mode="json", exclude_defaults=True),
    }


def observation_entry(obs: Observation) -> dict[str, Any]:
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


def _board_common(board: BlackboardReader) -> dict[str, Any]:
    return {
        "evidence": [evidence_entry(e) for e in board.evidence()],
        "data_gaps": [g.model_dump(mode="json") for g in board.data_gaps()],
        "claims": [claim_entry(c) for c in board.claims()],
        "evidence_requests": [request_entry(r) for r in board.evidence_requests()],
        "findings": [f.model_dump(mode="json") for f in board.findings()],
    }


def specialist_prompt(
    *,
    role: str,
    incident: IncidentReport,
    components: Sequence[str],
    task: dict[str, Any],
    observations: Sequence[Observation],
    tools: Sequence[ToolName],
    steps_remaining: int,
) -> str:
    context = {
        "role": role,
        "incident": incident_entry(incident),
        "components": sorted(components),
        "task": task,
        "observations": [observation_entry(o) for o in observations],
        "tools": tool_schemas(tools),
        "steps_remaining": steps_remaining,
    }
    return (
        f"Investigate as the {role} agent. Your task and your tool observations so far are in the context. "
        "Report findings that cite the evidence_id of each observation you rely on.\n\n" + render_context(context)
    )


def reasoning_prompt(board: BlackboardReader, incident: IncidentReport, components: Sequence[str]) -> str:
    context = {
        "role": "reasoning",
        "incident": incident_entry(incident),
        "components": sorted(components),
        "cause_types": [c.value for c in CauseType],
        "latest_hypotheses": [hypothesis_entry(h) for h in board.active_hypotheses()],
        "max_hypotheses": 5,
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
    context = {
        "role": "critic",
        "incident": incident_entry(incident),
        "components": sorted(components),
        "hypotheses": [hypothesis_entry(h) for h in board.active_hypotheses()],
        "critic_round": critic_round,
        "critic_round_budget": round_budget,
        "max_requests": max_requests,
        "concern_categories": [c.value for c in ConcernCategory],
        "specialist_tools": {agent.value: sorted(t.value for t in tools) for agent, tools in SPECIALIST_TOOLS.items()},
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
        "evidence": [evidence_entry(e) for e in board.evidence()],
        "findings": [f.model_dump(mode="json") for f in board.findings()],
        "data_gaps": [g.model_dump(mode="json") for g in board.data_gaps()],
        "missing_evidence": list(missing_evidence),
        "max_actions": max_actions,
        "action_categories": ["mitigation", "fix", "prevention", "further_investigation"],
    }
    return "Write the remediation recommendations for the final report.\n\n" + render_context(context)
