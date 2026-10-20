"""Scripted-run harness for the cross-cutting tests (task 9.1, Req 30.1, 30.4, 30.5).

Builds ``RunDependencies`` from a scripted Fake_LLM and a ``FakeClock``, writes a hand-built or
generated incident under a temporary Snapshot_Root, runs either system into a temporary output
directory and returns the report, trace, ledger, recorded prompts and captured Observations.

Only tests read Answer_Keys; nothing here is passed to a System_Under_Test except ``RunInputs``.
"""

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any, Literal

from incident_investigator.clock import FakeClock
from incident_investigator.llm.base import LLMRequest, LLMResponse
from incident_investigator.llm.context import extract_context
from incident_investigator.llm.fake import FakeScriptExhausted, ScriptedFakeLLM, ScriptEntry
from incident_investigator.llm.offline import OfflineResponder
from incident_investigator.models.answer_key import AnswerKey
from incident_investigator.models.blackboard import BlackboardState
from incident_investigator.models.config import Config, parse_config
from incident_investigator.models.evidence import Observation
from incident_investigator.models.incident import IncidentReport
from incident_investigator.models.report import InvestigationReport
from incident_investigator.models.run import RunInputs, RunResult
from incident_investigator.models.snapshot import IncidentSnapshot
from incident_investigator.models.trace import RunTrace
from incident_investigator.models.vocab import Caller, TerminationReason
from incident_investigator.redact import SecretRedactor
from incident_investigator.report.writer import (
    LEDGER_JSON,
    REPORT_JSON,
    REPORT_MD,
    TRACE_JSON,
    FileRunOutputSink,
    load_report,
)
from incident_investigator.runtime import RunDependencies
from incident_investigator.tools.layer import ToolLayer
from incident_investigator.tools.snapshot import FAILURE_PROFILE_FILE, INCIDENT_REPORT_FILE, SNAPSHOT_FILE

REPO = Path(__file__).resolve().parents[1]
SCENARIOS_DIR = REPO / "scenarios"

CATALOGUE: tuple[str, ...] = (
    "bad-config-deploy",
    "database-failover",
    "third-party-api-outage",
    "slow-memory-leak",
    "expired-tls-certificate",
    "cache-stampede",
    "load-balancer-misconfiguration",
    "disk-full",
    "logs-timeout",
    "insufficient-evidence",
    "feature-flag-rollout",
    "cascading-failure",
)
"""The 12 Scenario_Names of the design catalogue table (D6)."""

OUTPUT_FILES = (REPORT_JSON, REPORT_MD, TRACE_JSON, LEDGER_JSON)

System = Literal["multi", "baseline"]


# Incidents on disk


@dataclass(frozen=True)
class IncidentFixture:
    """One incident under a temporary Snapshot_Root, plus its Answer_Key when generated."""

    root: Path
    snapshot_root: Path
    answer_key_root: Path
    snapshot_dir: Path
    report: IncidentReport
    answer_key: AnswerKey | None = None

    @property
    def canary(self) -> str | None:
        return self.answer_key.canary if self.answer_key else None


def _roots(root: Path) -> tuple[Path, Path]:
    snapshot_root, answer_key_root = root / "snapshots", root / "answer_keys"
    snapshot_root.mkdir(parents=True, exist_ok=True)
    answer_key_root.mkdir(parents=True, exist_ok=True)
    return snapshot_root, answer_key_root


def write_incident_dir(root: Path, snapshot: IncidentSnapshot, report: IncidentReport) -> IncidentFixture:
    """Write a hand-built snapshot as ``<root>/snapshots/<incident_id>/`` in the loader's file layout."""
    snapshot_root, answer_key_root = _roots(root)
    target = snapshot_root / snapshot.incident_id
    target.mkdir()
    files = {
        SNAPSHOT_FILE: snapshot.model_dump(mode="json", exclude={"failure_profile"}),
        FAILURE_PROFILE_FILE: snapshot.failure_profile.model_dump(mode="json"),
        INCIDENT_REPORT_FILE: report.model_dump(mode="json"),
    }
    for name, data in files.items():
        (target / name).write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return IncidentFixture(root, snapshot_root, answer_key_root, target, report)


@cache
def _generate(scenario: str, seed: int):
    """Generation is deterministic (Req 22.6), so each (scenario, seed) is generated once per session."""
    from incident_investigator.datagen.catalogue import load_scenario_spec
    from incident_investigator.datagen.generator import generate

    return generate(load_scenario_spec(SCENARIOS_DIR / f"{scenario}.json"), seed)


def generated_incident(root: Path, scenario: str = "bad-config-deploy", seed: int = 42) -> IncidentFixture:
    """Generate one catalogue scenario with the Data_Generator into temporary roots."""
    from incident_investigator.datagen.generator import write_incident

    snapshot_root, answer_key_root = _roots(root)
    generated = _generate(scenario, seed)
    target = write_incident(generated, snapshot_root, answer_key_root)
    key_file = answer_key_root / f"{generated.snapshot.incident_id}.json"
    report = IncidentReport.model_validate_json((target / INCIDENT_REPORT_FILE).read_text(encoding="utf-8"))
    key = AnswerKey.model_validate_json(key_file.read_text(encoding="utf-8"))
    return IncidentFixture(root, snapshot_root, answer_key_root, target, report, key)


def tree_bytes(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# Configuration


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def run_config(incident: IncidentFixture, **sections: Any) -> Config:
    """A fake-provider Config for ``incident``. Random failure injection is off unless a test turns it on,
    so scripted Runs are predictable; ``sections`` deep-merge over these defaults."""
    base: dict[str, Any] = {
        "llm": {"provider": "fake", "model": "scripted-fake"},
        "tools": {"failure_injection": {"random_enabled": False}},
        "paths": {"snapshot_root": str(incident.snapshot_root), "answer_key_root": str(incident.answer_key_root)},
    }
    return parse_config(deep_merge(base, sections), source=Path("<harness>"))


# Fake_LLM


class MixedFakeLLM(ScriptedFakeLLM):
    """``ScriptedFakeLLM`` that answers unscripted calls with the ``OfflineResponder``.

    Scripted entries are consumed first (FIFO per ``(caller, call_kind)``, then per caller). Any call
    with no entry left goes to the fallback, so a test scripts only the calls it is about and the
    Specialists' and planner's calls keep working as those agents grow. ``fallback=False`` raises
    ``FakeScriptExhausted`` instead. Every prompt is recorded once, in order, with the Clock time it
    arrived (``timeline``).
    """

    def __init__(self, entries: Sequence[ScriptEntry], clock: FakeClock, *, fallback: bool = True):
        super().__init__(entries, clock)
        self._fallback = OfflineResponder() if fallback else None
        self._clock_ref = clock
        self.timeline: list[tuple[float, LLMRequest]] = []
        self.fallback_calls: list[LLMRequest] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.timeline.append((self._clock_ref.monotonic(), request))
        try:
            return await super().complete(request)
        except FakeScriptExhausted:
            if self._fallback is None:
                raise
            self.fallback_calls.append(request)
            return await self._fallback.complete(request)


def invalid(caller: Caller, n: int) -> list[ScriptEntry]:
    """``n`` unparseable outputs for ``caller`` (any call kind, so repairs consume them too)."""
    return [ScriptEntry(caller, text="not json") for _ in range(n)]


def context_of(request: LLMRequest) -> dict[str, Any]:
    return extract_context(request.user)


Responder = Callable[[LLMRequest], str]


def reasoning_reply(*, confidence: float = 0.9, runner_up: float = 0.2, cause_type: str = "config_error") -> Responder:
    """Two Hypotheses: rank 1 on the affected service citing its complete, current evidence; rank 2 on
    another Component with no support. Every Incident_Report claim stays ``unverified``."""

    def respond(request: LLMRequest) -> str:
        ctx = context_of(request)
        affected = ctx["incident"]["affected_service"]
        other = next(c for c in sorted(ctx["components"]) if c != affected)
        current = [e for e in ctx.get("evidence", []) if e["reliability"] == "complete" and not e["historical"]]
        on_affected = [e["evidence_id"] for e in current if e["query"].get("service") == affected]
        support = (on_affected or [e["evidence_id"] for e in current])[:2]
        hypotheses = [
            {
                "component": affected,
                "cause_type": cause_type,
                "statement": f"{cause_type.replace('_', ' ')} on {affected}",
                "supporting_evidence_ids": support,
                "confidence": confidence,
                "rank": 1,
                "evidence_sufficient": bool(support),
            },
            {
                "component": other,
                "cause_type": "dependency_timeout",
                "statement": f"dependency timeout on {other}",
                "confidence": runner_up,
                "rank": 2,
                "evidence_sufficient": False,
                "missing_evidence": [{"description": f"Error rate of {other}", "suggested_tool": "query_metrics"}],
            },
        ]
        statuses = [{"claim_id": c["claim_id"], "status": "unverified"} for c in ctx.get("claims", [])]
        return json.dumps({"hypotheses": hypotheses, "claim_statuses": statuses})

    return respond


def accept_rank1(request: LLMRequest) -> str:
    hypotheses = context_of(request)["hypotheses"]
    return json.dumps({"accept_hypothesis_id": min(hypotheses, key=lambda h: h["rank"])["hypothesis_id"]})


def evidence_request(target: str, ctx: dict[str, Any], *, text: str = "timeout") -> dict[str, Any]:
    """One valid Evidence_Request for the rank-1 Hypothesis, derived only from the prompt context."""
    rank1 = min(ctx["hypotheses"], key=lambda h: h["rank"])
    affected = ctx["incident"]["affected_service"]
    start = datetime.fromisoformat(ctx["incident"]["start_time"])
    if target == "observability":
        tool = "query_logs"
        window = {"start": (start - timedelta(hours=1)).isoformat(), "end": (start + timedelta(hours=1)).isoformat()}
        params: dict[str, Any] = {"service": affected, "time_range": window, "levels": ["error"], "text": text, "limit": 20}
    else:
        tool = "get_deployments"
        window = {"start": (start - timedelta(days=2)).isoformat(), "end": start.isoformat()}
        params = {"service": affected, "time_range": window, "limit": 20}
    return {
        "target": target,
        "hypothesis_id": rank1["hypothesis_id"],
        "concern": "missing_evidence",
        "question": f"What does {tool} show for {affected} around the start time?",
        "suggested_tool": tool,
        "suggested_params": params,
        "rationale": "The top hypothesis needs more current evidence.",
    }


def request_evidence(*targets: str, text: str = "timeout") -> Responder:
    """A Critic reply filing one Evidence_Request per target (``observability`` or ``dependency``)."""

    def respond(request: LLMRequest) -> str:
        ctx = context_of(request)
        return json.dumps({"evidence_requests": [evidence_request(t, ctx, text=text) for t in targets]})

    return respond


# Running a system


@contextmanager
def capture_observations() -> Iterator[list[Observation]]:
    """Record every Observation the Tool_Layer returns, for either system."""
    seen: list[Observation] = []
    original = ToolLayer.request

    async def recording(self, *args, **kwargs):
        observation = await original(self, *args, **kwargs)
        seen.append(observation)
        return observation

    ToolLayer.request = recording
    try:
        yield seen
    finally:
        ToolLayer.request = original


def _runner(system: System):
    if system == "multi":
        from incident_investigator.orchestrator.run import run_investigator

        return run_investigator
    from incident_investigator.baseline.react import run_baseline  # ws/eval-baseline

    return run_baseline


@dataclass
class RunOutcome:
    system: System
    inputs: RunInputs
    result: RunResult
    prompts: list[LLMRequest]
    observations: list[Observation]
    out_dir: Path
    writes: int
    clock: FakeClock
    llm: Any = field(repr=False, default=None)

    @property
    def report(self) -> InvestigationReport:
        return self.result.report

    @property
    def trace(self) -> RunTrace:
        return self.result.trace

    @property
    def ledger(self) -> BlackboardState:
        return self.result.ledger

    def events(self, event_type: str) -> tuple:
        return self.trace.of_type(event_type)

    def files(self) -> dict[str, str]:
        return {name: (self.out_dir / name).read_text(encoding="utf-8") for name in OUTPUT_FILES}

    def prompts_by(self, caller: Caller) -> list[LLMRequest]:
        return [p for p in self.prompts if str(p.caller) == str(caller)]

    def task(self, task_id: str):
        return next(t for t in self.ledger.tasks if t.task_id == task_id)

    def artifact_texts(self) -> dict[str, str]:
        """Every artifact Req 24.6 names: prompts (with repairs), Tool_Results, state, trace and both reports."""
        texts = {f"prompt[{i}] {p.caller}/{p.call_kind}": f"{p.system}\n{p.user}" for i, p in enumerate(self.prompts)}
        texts.update({f"observation[{i}]": o.model_dump_json() for i, o in enumerate(self.observations)})
        texts["ledger state"] = self.ledger.model_dump_json()
        texts["trace"] = self.trace.model_dump_json()
        texts["run inputs"] = self.inputs.model_dump_json()
        texts.update(self.files())
        return texts


async def run_system(
    system: System,
    incident: IncidentFixture,
    llm: Any,
    out_dir: Path,
    *,
    config: Config | None = None,
    clock: FakeClock | None = None,
    seed: int | None = None,
) -> RunOutcome:
    """Run ``system`` on ``incident`` with ``llm`` and write the four output files to ``out_dir``."""
    config = config or run_config(incident)
    clock = clock or FakeClock()
    inputs = RunInputs(
        snapshot_dir=incident.snapshot_dir,
        incident_report=incident.report,
        config=config.for_system_under_test(seed),
    )
    sink = FileRunOutputSink(out_dir, SecretRedactor())
    deps = RunDependencies(llm_client=llm, clock=clock, redactor=SecretRedactor())
    with capture_observations() as observations:
        result = await _runner(system)(inputs, deps, sink)
    return RunOutcome(
        system=system,
        inputs=inputs,
        result=result,
        prompts=list(llm.prompts),
        observations=list(observations),
        out_dir=Path(out_dir),
        writes=sink.written,
        clock=clock,
        llm=llm,
    )


async def run_scripted(
    system: System,
    incident: IncidentFixture,
    out_dir: Path,
    script: Sequence[ScriptEntry] = (),
    *,
    fallback: bool = True,
    **sections: Any,
) -> RunOutcome:
    """Run ``system`` with a ``MixedFakeLLM`` built from ``script``; ``sections`` override the config."""
    clock = FakeClock()
    llm = MixedFakeLLM(script, clock, fallback=fallback)
    return await run_system(system, incident, llm, out_dir, config=run_config(incident, **sections), clock=clock)


# Shared assertions


def assert_single_report(outcome: RunOutcome, reason: TerminationReason) -> None:
    """Req 30.6: exactly one report, and the report, the Markdown and the trace carry ``reason``."""
    assert outcome.writes == 1, f"expected exactly one report write, got {outcome.writes}"
    assert all((outcome.out_dir / name).is_file() for name in OUTPUT_FILES)
    on_disk = load_report(outcome.out_dir / REPORT_JSON)
    assert on_disk == outcome.report
    assert outcome.report.termination_reason is reason, outcome.report.termination_reason
    assert f"`{reason}`" in outcome.files()[REPORT_MD]
    terminations = outcome.events("termination")
    assert [e.reason for e in terminations] == [reason], [e.reason for e in terminations]
    assert [e.termination_reason for e in outcome.events("run_ended")] == [reason]
    assert outcome.ledger.termination is not None and outcome.ledger.termination.reason is reason


def distinct_gap_count(report: InvestigationReport) -> int:
    return len({g.dedupe_key() for g in report.data_gaps if not g.after_verdict})
