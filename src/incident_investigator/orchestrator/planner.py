"""Planner: one ``plan`` LLM call, validated, else the Default_Task_Graph (Req 6.1, 6.3, 6.4, 6.7, 6.8).

The plan prompt here is minimal; the agents workstream owns the planner prompt text (task 7.6).
"""

from typing import Literal

from ..agents.prompts import templates
from ..llm.base import StructuredLLM, ValidationContext
from ..llm.context import render_context
from ..llm.validator import StructuredOutputFailure
from ..models.incident import IncidentReport
from ..models.outputs import PlanOutput
from ..models.vocab import SPECIALIST_TOOLS, AgentName, CallKind
from ..tracing import TraceRecorder
from .graph import TaskFactory, TaskGraph

FallbackCause = Literal["cycle", "duplicate_task_id", "unknown_dependency", "structure", "validation_failed", "llm_failed"]

DAG_RULES = (
    "Every task_id is unique. depends_on lists task_ids of this plan only and the graph has no cycles.",
    "Observability and dependency tasks may depend only on other observability or dependency tasks.",
    "The single reasoning task depends on every observability and dependency task and nothing else.",
    "The single critic task depends on the reasoning task only.",
    "The single reporting task has no dependencies; it runs after the feedback loop ends.",
)


def plan_prompt(incident: IncidentReport, max_initial: int) -> str:
    context = {
        "role": "orchestrator",
        "incident": incident.model_dump(mode="json"),
        "specialist_tools": {agent.value: sorted(t.value for t in tools) for agent, tools in SPECIALIST_TOOLS.items()},
        "agents": [a.value for a in AgentName if a is not AgentName.ORCHESTRATOR],
        "max_initial_tasks": max_initial,
        "dag_rules": list(DAG_RULES),
    }
    return "Plan the initial investigation task graph for this incident.\n\n" + render_context(context)


class Planner:
    def __init__(self, llm: StructuredLLM, trace: TraceRecorder, max_initial: int):
        self._llm = llm
        self._trace = trace
        self._max_initial = max_initial

    async def plan(self, incident: IncidentReport, factory: TaskFactory) -> tuple[TaskGraph, FallbackCause | None]:
        """The validated planned graph, or the Default_Task_Graph and the cause of the fallback."""
        try:
            plan = await self._llm.structured(
                AgentName.ORCHESTRATOR,
                CallKind.PLAN,
                templates.PLANNER,
                plan_prompt(incident, self._max_initial),
                PlanOutput,
                ValidationContext(),
            )
        except StructuredOutputFailure as exc:
            return self._fallback(factory, "validation_failed", str(exc))
        except Exception as exc:  # provider error, blocked call or a fake without a plan rule: Req 6.4
            return self._fallback(factory, "llm_failed", f"{type(exc).__name__}: {exc}")
        defect = TaskGraph.validate_plan(plan, self._max_initial)
        if defect is not None:
            return self._fallback(factory, defect.cause, defect.detail)
        return TaskGraph.from_plan(plan, factory), None

    def _fallback(self, factory: TaskFactory, cause: FallbackCause, detail: str) -> tuple[TaskGraph, FallbackCause]:
        self._trace.record("default_graph_fallback", cause=cause, detail=detail[:2000])
        return TaskGraph.default(factory), cause
