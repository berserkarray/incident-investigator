"""Baseline_Agent prompt: run-time content plus one ``<context>`` JSON block (design D5, prompt table).

The context carries the Incident_Report, the claims to verify, all five Tool schemas, the valid
Components, the default query windows the Specialists also start from, and the full step history
(reasoning notes plus Observations in the shared format of Req 25.10).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from ..agents.prompts.builders import claim_entry, incident_entry, observation_entry, tool_schemas
from ..agents.specialist import OPENING_LIMIT, OPENING_WINDOW_AFTER
from ..llm.context import render_context
from ..models.config import InvestigatorConfig
from ..models.evidence import Observation
from ..models.incident import IncidentClaim, IncidentReport
from ..models.vocab import ActionCategory, CauseType, ToolName


@dataclass(frozen=True)
class StepRecord:
    """One completed Tool_Request step: the model's note and the Observation it got back."""

    step: int
    reasoning_note: str
    observation: Observation


def suggested_windows(incident: IncidentReport, config: InvestigatorConfig) -> dict[str, dict[str, str]]:
    """The same default windows the Specialist opening requests use, so both systems start alike."""
    start = incident.start_time
    margin = timedelta(seconds=config.orchestrator.pre_incident_margin_s)
    lookback = timedelta(seconds=config.orchestrator.change_lookback_s)
    return {
        "telemetry": {"start": (start - margin).isoformat(), "end": (start + OPENING_WINDOW_AFTER).isoformat()},
        "changes": {"start": (start - lookback).isoformat(), "end": start.isoformat()},
    }


def baseline_prompt(
    *,
    incident: IncidentReport,
    claims: Sequence[IncidentClaim],
    components: Sequence[str],
    history: Sequence[StepRecord],
    config: InvestigatorConfig,
    step: int,
    tool_calls_used: int,
) -> str:
    context: dict[str, Any] = {
        "role": "baseline",
        "incident": incident_entry(incident),
        "claims": [claim_entry(c) for c in claims],
        "components": sorted(components),
        "cause_types": [c.value for c in CauseType],
        "tools": tool_schemas(list(ToolName)),
        "suggested_windows": suggested_windows(incident, config),
        "suggested_limit": OPENING_LIMIT,
        "history": [
            {"step": h.step, "reasoning_note": h.reasoning_note, "observation": observation_entry(h.observation)}
            for h in history
        ],
        "step": step,
        "tool_calls_used": tool_calls_used,
        "tool_call_budget": config.budgets.tool_calls,
        "max_hypotheses": 5,
        "max_actions": config.report.max_recommended_actions,
        "action_categories": [c.value for c in ActionCategory],
    }
    return (
        "Investigate the incident. Your earlier steps and their observations are in context.history. Either "
        "request one more tool or give the final answer, citing the evidence_id of each observation you rely on."
        "\n\n" + render_context(context)
    )
