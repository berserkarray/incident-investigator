"""Scripted Investigator Runs on a hand-built snapshot with a FakeClock and a ScriptedFakeLLM.

The cross-cutting harness of task 9.1 supersedes this; it stays small and local to the orchestrator tests.
"""

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from factories import make_config, make_incident_report, make_snapshot

from incident_investigator.clock import FakeClock
from incident_investigator.llm.base import LLMRequest
from incident_investigator.llm.context import extract_context
from incident_investigator.llm.fake import ScriptedFakeLLM, ScriptEntry
from incident_investigator.models.budget import TokenCount
from incident_investigator.models.incident import IncidentReport
from incident_investigator.models.run import RunInputs, RunResult
from incident_investigator.models.snapshot import IncidentSnapshot
from incident_investigator.models.vocab import AgentName, CallKind
from incident_investigator.orchestrator.run import run_investigator
from incident_investigator.report.writer import MemoryRunOutputSink
from incident_investigator.runtime import RunDependencies

OBS, DEP, REA, CRI, REP, ORC = (
    AgentName.OBSERVABILITY,
    AgentName.DEPENDENCY,
    AgentName.REASONING,
    AgentName.CRITIC,
    AgentName.REPORTING,
    AgentName.ORCHESTRATOR,
)
WINDOW = {"start": "2024-01-01T00:00:00Z", "end": "2024-01-01T02:00:00Z"}


def ctx_of(request: LLMRequest) -> dict[str, Any]:
    return extract_context(request.user)


def entries(caller, kind, respond: Callable[[LLMRequest], str] | str, n: int = 1, **extra) -> list[ScriptEntry]:
    key = "respond" if callable(respond) else "text"
    return [ScriptEntry(caller, kind, **{key: respond}, **extra) for _ in range(n)]


def no_findings(request: LLMRequest) -> str:
    """Empty findings, as a ``specialist_step`` answer or as the final ``specialist_findings`` answer."""
    if request.call_kind is CallKind.SPECIALIST_STEP:
        return json.dumps({"reasoning": "Nothing further to query.", "findings": {"claims": []}})
    return json.dumps({"claims": []})


def reasoning_reply(request: LLMRequest) -> str:
    """Two Hypotheses; rank 1 cites the first current Evidence_Item; every claim stays unverified."""
    ctx = ctx_of(request)
    current = [e["evidence_id"] for e in ctx["evidence"] if not e["historical"]]
    return json.dumps(
        {
            "hypotheses": [
                {
                    "component": "checkout-a1b",
                    "cause_type": "config_error",
                    "statement": "A configuration change broke checkout.",
                    "supporting_evidence_ids": current[:1],
                    "confidence": 0.9,
                    "rank": 1,
                    "evidence_sufficient": True,
                },
                {
                    "component": "payments-c2d",
                    "cause_type": "dependency_timeout",
                    "statement": "Payments became slow.",
                    "confidence": 0.3,
                    "rank": 2,
                    "evidence_sufficient": False,
                },
            ],
            "claim_statuses": [{"claim_id": c["claim_id"], "status": "unverified"} for c in ctx["claims"]],
        }
    )


def accept_reply(request: LLMRequest) -> str:
    rank1 = min(ctx_of(request)["hypotheses"], key=lambda h: h["rank"])
    return json.dumps({"accept_hypothesis_id": rank1["hypothesis_id"]})


def logs_request(**params: Any) -> dict[str, Any]:
    return {
        "target": "observability",
        "concern": "missing_evidence",
        "question": "Do checkout logs show errors after the change?",
        "suggested_tool": "query_logs",
        "suggested_params": {"service": "checkout-a1b", "time_range": WINDOW, "levels": ["error"], "limit": 25, **params},
        "rationale": "The top hypothesis needs more current evidence.",
    }


def deployments_request() -> dict[str, Any]:
    return {
        "target": "dependency",
        "concern": "temporal_coincidence",
        "question": "Which changes landed before the onset?",
        "suggested_tool": "get_deployments",
        "suggested_params": {"service": "checkout-a1b", "time_range": WINDOW, "limit": 25},
        "rationale": "A change near the onset may be a coincidence.",
    }


def requests_reply(*drafts: dict[str, Any]) -> Callable[[LLMRequest], str]:
    def respond(request: LLMRequest) -> str:
        rank1 = min(ctx_of(request)["hypotheses"], key=lambda h: h["rank"])
        return json.dumps({"evidence_requests": [{**d, "hypothesis_id": rank1["hypothesis_id"]} for d in drafts]})

    return respond


NO_ACTIONS = json.dumps({"summary_claims": [], "recommended_actions": []})


def usage(n: int) -> TokenCount:
    return TokenCount(input_tokens=n, output_tokens=0)


def base_script(*, critic: Sequence[ScriptEntry] = (), reasoning_rounds: int = 1, reporting: bool = True) -> list[ScriptEntry]:
    """Findings for every Observability Task, Reasoning per round, the given Critic entries, Reporting."""
    script = [
        # Specialists run code-issued requests, then a specialist_step loop (ws/agents); each step here
        # returns empty findings at once. call_kind=None serves both step and final-findings calls.
        *entries(OBS, None, no_findings, n=4),
        *entries(DEP, None, no_findings, n=4),
        *entries(REA, CallKind.REASONING, reasoning_reply, n=reasoning_rounds),
        *critic,
    ]
    if reporting:
        script += entries(REP, CallKind.REPORTING, NO_ACTIONS)
    return script


def write_snapshot(root: Path, snapshot: IncidentSnapshot, report: IncidentReport) -> Path:
    directory = root / "snapshots" / snapshot.incident_id
    directory.mkdir(parents=True)
    (directory / "snapshot.json").write_text(snapshot.model_dump_json(exclude={"failure_profile"}))
    (directory / "failure_profile.json").write_text(snapshot.failure_profile.model_dump_json())
    (directory / "incident_report.json").write_text(report.model_dump_json())
    return directory


@dataclass
class ScriptedRun:
    result: RunResult
    fake: ScriptedFakeLLM
    sink: MemoryRunOutputSink
    clock: FakeClock

    @property
    def report(self):
        return self.result.report

    @property
    def trace(self):
        return self.result.trace

    def events(self, event_type: str) -> tuple:
        return self.trace.of_type(event_type)

    def transitions(self) -> list[tuple[str, str, str]]:
        return [(e.task_id, str(e.from_state), str(e.to_state)) for e in self.events("task_transition")]

    def kinds(self) -> list[CallKind]:
        return [p.call_kind for p in self.fake.prompts]


async def run_scripted(tmp_path: Path, script: Sequence[ScriptEntry], **config_sections: Any) -> ScriptedRun:
    snapshot, report = make_snapshot(), make_incident_report()
    snapshot_dir = write_snapshot(tmp_path, snapshot, report)
    paths = {"snapshot_root": str(tmp_path / "snapshots"), "answer_key_root": str(tmp_path / "keys")}
    # Scripted runs turn random failure injection off so every Tool_Call is predictable.
    config_sections.setdefault("tools", {"failure_injection": {"random_enabled": False}})
    config = make_config(paths=paths, **config_sections).for_system_under_test()
    clock = FakeClock()
    fake = ScriptedFakeLLM(script, clock)
    sink = MemoryRunOutputSink()
    result = await run_investigator(
        RunInputs(snapshot_dir=snapshot_dir, incident_report=report, config=config),
        RunDependencies(llm_client=fake, clock=clock),
        sink,
    )
    assert len(sink.outputs) == 1, "every Run writes exactly one report"
    return ScriptedRun(result=result, fake=fake, sink=sink, clock=clock)


OPENING_REQUESTS = 4
"""Code-issued Tool_Requests of the first Specialist Tasks: query_logs and query_metrics (Observability),
get_deployments and get_dependencies (Dependency) (Req 8.2, 9.2, 9.6)."""
