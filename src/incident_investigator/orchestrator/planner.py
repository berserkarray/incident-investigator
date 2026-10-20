"""Planner: one ``plan`` LLM call, validated, else the Default_Task_Graph (Req 6.1, 6.3, 6.4, 6.7, 6.8).

The prompt and the repairable content rules come from ``agents.planner.request_plan``. The structural
rules of Req 6.1 are checked here by ``TaskGraph.validate_plan``; a structural defect, a planning
output that still fails validation after its repair, or a failed LLM call all fall back to the
Default_Task_Graph with one ``default_graph_fallback`` trace event naming the cause.
"""

from collections.abc import Sequence
from typing import Literal

from ..agents.planner import request_plan
from ..llm.base import StructuredLLM
from ..llm.validator import StructuredOutputFailure
from ..models.config import OrchestratorSettings
from ..models.incident import IncidentReport
from ..tracing import TraceRecorder
from .graph import TaskFactory, TaskGraph

FallbackCause = Literal["cycle", "duplicate_task_id", "unknown_dependency", "structure", "validation_failed", "llm_failed"]


class Planner:
    def __init__(
        self,
        llm: StructuredLLM,
        trace: TraceRecorder,
        settings: OrchestratorSettings,
        components: Sequence[str] = (),
    ):
        self._llm = llm
        self._trace = trace
        self._settings = settings
        self._components = tuple(sorted(components))

    async def plan(self, incident: IncidentReport, factory: TaskFactory) -> tuple[TaskGraph, FallbackCause | None]:
        """The validated planned graph, or the Default_Task_Graph and the cause of the fallback."""
        try:
            plan = await request_plan(self._llm, incident, self._components, self._settings)
        except StructuredOutputFailure as exc:
            return self._fallback(factory, "validation_failed", str(exc))
        except Exception as exc:  # provider error or a blocked call (Req 6.4)
            return self._fallback(factory, "llm_failed", f"{type(exc).__name__}: {exc}")
        defect = TaskGraph.validate_plan(plan, self._settings.max_initial_tasks_per_specialist)
        if defect is not None:
            return self._fallback(factory, defect.cause, defect.detail)
        return TaskGraph.from_plan(plan, factory), None

    def _fallback(self, factory: TaskFactory, cause: FallbackCause, detail: str) -> tuple[TaskGraph, FallbackCause]:
        self._trace.record("default_graph_fallback", cause=cause, detail=detail[:2000])
        return TaskGraph.default(factory), cause
