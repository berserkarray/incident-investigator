"""The Orchestrator's planning call: prompt, ``PlanOutput`` context rules and one helper (Req 6.1, 6.4).

Two kinds of rule apply to a plan, and they fail differently:

- The Task_Graph structure of Req 6.1 (agent counts, the Reasoning/Critic/Reporting edges, cycles,
  duplicate or dangling IDs) is checked by the Orchestrator's ``validate_plan`` after this call. A
  structural defect discards the whole plan in favour of the Default_Task_Graph with a trace event
  naming the defect (Req 6.3, 6.8); it is deliberately not repaired here.
- The context rules below concern the content of individual planned Tasks. They are repairable like
  schema errors; a second failure is a planning failure, which also falls back (Req 6.4).
"""

from collections.abc import Sequence
from datetime import datetime, timedelta

from pydantic import BaseModel

from ..llm.base import SemanticCheck, StructuredLLM, ValidationContext
from ..models.config import OrchestratorSettings
from ..models.incident import IncidentReport
from ..models.outputs import PlanOutput
from ..models.vocab import SPECIALIST_TOOLS, AgentName, CallKind
from .prompts import templates
from .prompts.builders import plan_prompt
from .specialist import OPENING_WINDOW_AFTER


def plan_window(incident: IncidentReport, settings: OrchestratorSettings) -> tuple[datetime, datetime]:
    """The period a planned Specialist time range must overlap: from the earliest code-issued query
    (the change lookback or the pre-incident margin) to the end of the opening window."""
    start = incident.start_time
    reach_back = max(settings.change_lookback_s, settings.pre_incident_margin_s)
    return start - timedelta(seconds=reach_back), start + OPENING_WINDOW_AFTER


def plan_check(incident: IncidentReport, settings: OrchestratorSettings) -> SemanticCheck:
    """Context rules for one ``PlanOutput``:

    - every Specialist Task states a non-blank focus, which becomes its Task brief;
    - two Specialist Tasks of the same agent differ in focus or time range;
    - only Specialist Tasks carry a time range, since no other agent makes Tool_Requests;
    - a Specialist time range overlaps ``plan_window``.
    """
    window_start, window_end = plan_window(incident, settings)

    def check(output: BaseModel, vctx: ValidationContext) -> list[str]:
        assert isinstance(output, PlanOutput)
        problems: list[str] = []
        seen: dict[tuple, int] = {}
        for i, task in enumerate(output.tasks):
            if task.agent not in SPECIALIST_TOOLS:
                if task.time_range is not None:
                    problems.append(f"tasks.{i}.time_range: only observability and dependency tasks take a time range")
                continue
            if not task.focus.strip():
                problems.append(f"tasks.{i}.focus: say what this {task.agent} task should establish")
            key = (task.agent, task.focus.strip().casefold(), task.time_range)
            if key in seen:
                problems.append(f"tasks.{i}: repeats tasks.{seen[key]} (same agent, focus and time range)")
            seen.setdefault(key, i)
            if task.time_range is not None and not (
                task.time_range.start < window_end and window_start < task.time_range.end
            ):
                problems.append(
                    f"tasks.{i}.time_range: must overlap [{window_start.isoformat()}, {window_end.isoformat()})"
                )
        return problems

    return check


async def request_plan(
    llm: StructuredLLM,
    incident: IncidentReport,
    components: Sequence[str],
    settings: OrchestratorSettings,
) -> PlanOutput:
    """One ``plan`` call (plus at most one repair). Raises ``StructuredOutputFailure`` or ``LLMCallError``
    on failure; the caller then uses the Default_Task_Graph (Req 6.4)."""
    user = plan_prompt(incident, components, max_initial_tasks=settings.max_initial_tasks_per_specialist)
    vctx = ValidationContext(valid_components=frozenset(components))
    return await llm.structured(
        AgentName.ORCHESTRATOR,
        CallKind.PLAN,
        templates.PLANNER,
        user,
        PlanOutput,
        vctx,
        plan_check(incident, settings),
    )
