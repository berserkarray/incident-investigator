"""Baseline_Agent prompt: run-time content plus one ``<context>`` JSON block (design D5, prompt table).

The context carries the Incident_Report, the claims to verify, all five Tool schemas with their closed
vocabularies (as the Specialists get them), the valid Components, the default query windows the
Specialists also start from, the remaining budgets, and the full step history: the opening
Observations the harness collected before step 1 (Req 25.11), then each step's reasoning note,
Tool_Request and Observation in the shared format of Req 25.10.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ..agents.opening import OPENING_LIMIT, change_window, telemetry_window
from ..agents.prompts.builders import claim_entry, incident_entry, observation_entry, tool_schemas, tool_vocabularies
from ..llm.context import render_context
from ..models.config import InvestigatorConfig
from ..models.evidence import Observation
from ..models.incident import IncidentClaim, IncidentReport
from ..models.vocab import ActionCategory, CauseType, ToolName

OPENING_NOTE = "Opening request collected before the first step (the same first requests the specialists make)."


@dataclass(frozen=True)
class StepRecord:
    """One Tool_Request in the history: an opening request (step 0) or a step's request, with its Observation."""

    step: int
    reasoning_note: str
    observation: Observation
    opening: bool = False


def suggested_windows(incident: IncidentReport, config: InvestigatorConfig) -> dict[str, dict[str, str]]:
    """The same default windows the Specialist opening requests use, so both systems start alike."""
    settings = config.orchestrator
    return {
        "telemetry": telemetry_window(incident, settings).model_dump(mode="json"),
        "changes": change_window(incident, settings).model_dump(mode="json"),
    }


def history_entry(record: StepRecord) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "step": record.step,
        "reasoning_note": record.reasoning_note,
        "observation": observation_entry(record.observation),
    }
    if record.opening:
        entry["opening"] = True
    return entry


def baseline_prompt(
    *,
    incident: IncidentReport,
    claims: Sequence[IncidentClaim],
    components: Sequence[str],
    history: Sequence[StepRecord],
    config: InvestigatorConfig,
    step: int,
    tool_calls_used: int,
    tokens_used: int = 0,
) -> str:
    tools = list(ToolName)
    context: dict[str, Any] = {
        "role": "baseline",
        "incident": incident_entry(incident),
        "claims": [claim_entry(c) for c in claims],
        "components": sorted(components),
        "cause_types": [c.value for c in CauseType],
        "tools": tool_schemas(tools),
        "vocabularies": tool_vocabularies(tools),
        "suggested_windows": suggested_windows(incident, config),
        "suggested_limit": OPENING_LIMIT,
        "history": [history_entry(h) for h in history],
        "step": step,
        "tool_calls_used": tool_calls_used,
        "tool_call_budget": config.budgets.tool_calls,
        "tokens_used": tokens_used,
        "token_budget": config.budgets.tokens,
        "max_hypotheses": 5,
        "max_actions": config.report.max_recommended_actions,
        "action_categories": [c.value for c in ActionCategory],
    }
    return (
        "Investigate the incident. Your evidence so far is in context.history, starting with the opening "
        "observations. Either request one more tool or give the final answer, citing the evidence_id of each "
        "observation you rely on.\n\n" + render_context(context)
    )
