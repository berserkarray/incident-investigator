"""Evaluator runner: every selected scenario x run index x system, one Run at a time (Req 24.4, 24.7, 26).

The Evaluator is the only non-test code that reads the Answer_Key_Root. A Run receives only the
``InvestigatorConfig`` projection (no Answer_Key_Root field), the snapshot path and the Incident_Report;
no Answer_Key content and no scenario name (Req 24.7, 26.10). Any exception in a Run is recorded as a
failed Run and the evaluation continues (Req 26.7).
"""

import json
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from ..clock import Clock
from ..llm.base import LLMClient, LLMRequest, LLMResponse
from ..models.answer_key import AnswerKey
from ..models.budget import TokenCount
from ..models.config import Config
from ..models.incident import IncidentReport
from ..models.results import ResultsTable, RunScore
from ..models.run import RunInputs, RunResult
from ..models.vocab import SystemName
from ..redact import SecretRedactor
from ..report.writer import LEDGER_JSON, REPORT_JSON, REPORT_MD, TRACE_JSON, FileRunOutputSink, RunOutputSink
from ..runtime import RunDependencies
from ..tools.snapshot import IncidentReportError, PathGuard, SnapshotLoader
from .isolation import IsolationScanner
from .metrics import SYSTEM_ORDER, RunIdentity, TraceCounts, aggregate, failed_run_score, score_run
from .results import write_results

RUN_FILES = (REPORT_JSON, REPORT_MD, TRACE_JSON, LEDGER_JSON)


def run_seed(config_seed: int, scenario: str, run_index: int) -> int:
    """A Run's Seed: a function of only the configured Seed, the scenario and the run index (Req 26.2)."""
    return int.from_bytes(sha256(f"{config_seed}|{scenario}|{run_index}".encode()).digest()[:8], "big")


def run_dir(out_dir: Path, scenario: str, system: SystemName, run_index: int) -> Path:
    """Per-Run output location; no two Runs share one (Req 26.4)."""
    return Path(out_dir) / "runs" / scenario / system.value / f"run-{run_index}"


@dataclass(frozen=True)
class EvalPlan:
    scenarios: tuple[str, ...]
    systems: tuple[SystemName, ...] = SYSTEM_ORDER
    runs: int = 3

    def ordered_systems(self) -> tuple[SystemName, ...]:
        """The Investigator before the Baseline_Agent (Req 26.3)."""
        return tuple(s for s in SYSTEM_ORDER if s in self.systems)


class RunHandle(Protocol):
    async def execute(self) -> RunResult: ...


SystemFactory = Callable[[SystemName, RunInputs, RunDependencies, RunOutputSink], RunHandle]


def default_system_factory(
    system: SystemName, inputs: RunInputs, deps: RunDependencies, sink: RunOutputSink
) -> RunHandle:
    if system is SystemName.MULTI:
        from ..orchestrator.run import InvestigatorRun

        return InvestigatorRun(inputs, deps, sink)
    from ..baseline.react import BaselineRun

    return BaselineRun(inputs, deps, sink)


class CountingLLMClient:
    """Wraps a Run's LLM client: sums provider usage and keeps each prompt for the isolation scan.

    The sums let a failed Run still report the tokens it used before the failure (Req 27.12).
    """

    def __init__(self, inner: LLMClient):
        self.inner = inner
        self.usage = TokenCount()
        self.prompts: list[str] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.prompts.append(f"{request.system}\n{request.user}")
        response = await self.inner.complete(request)
        self.usage = self.usage.plus(response.usage)
        return response


class TimedRunOutputSink(FileRunOutputSink):
    """Notes the Clock time at which ``report.json`` is written; latency ends there (Req 27.6)."""

    def __init__(self, out_dir: Path, redactor: SecretRedactor, clock: Clock):
        super().__init__(out_dir, redactor)
        self._clock = clock
        self.json_written_at: float | None = None

    def _write(self, name: str, text: str) -> None:
        super()._write(name, text)
        if name == REPORT_JSON:
            self.json_written_at = self._clock.monotonic()


class AnswerKeyError(Exception):
    pass


class AnswerKeyStore:
    """Reads every Answer_Key under the Answer_Key_Root and finds the one for a scenario."""

    def __init__(self, root: Path, prefer_seed: int):
        self.root = Path(root)
        self._prefer_seed = prefer_seed
        self._keys: list[AnswerKey] = []
        self._unreadable: list[str] = []
        for path in sorted(self.root.glob("*.json")) if self.root.is_dir() else ():
            try:
                self._keys.append(AnswerKey.model_validate(json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
                self._unreadable.append(f"{path.name} ({type(exc).__name__})")

    def for_scenario(self, scenario: str) -> AnswerKey:
        found = [k for k in self._keys if k.scenario_name == scenario]
        if len(found) > 1:
            found = [k for k in found if k.seed == self._prefer_seed]
        if len(found) == 1:
            return found[0]
        unreadable = f"; unreadable answer keys: {', '.join(self._unreadable)}" if self._unreadable else ""
        if not found:
            raise AnswerKeyError(f"no answer key for scenario {scenario} under {self.root}{unreadable}")
        raise AnswerKeyError(f"several answer keys for scenario {scenario} under {self.root} and none or many with seed {self._prefer_seed}")


@dataclass(frozen=True)
class _Scenario:
    name: str
    key: AnswerKey | None = None
    snapshot_dir: Path | None = None
    report: IncidentReport | None = None
    error: str | None = None


class EvaluationRunner:
    def __init__(
        self,
        config: Config,
        *,
        client_factory: Callable[[], LLMClient],
        clock: Clock,
        redactor: SecretRedactor | None = None,
        system_factory: SystemFactory = default_system_factory,
        eval_command: str = "incident-investigator eval",
        progress: Callable[[RunScore], None] | None = None,
    ):
        self.config = config
        self._client_factory = client_factory
        self._clock = clock
        self._redactor = redactor or SecretRedactor()
        self._system_factory = system_factory
        self._eval_command = eval_command
        self._progress = progress

    def _prepare(self, scenario: str, keys: AnswerKeyStore, loader: SnapshotLoader) -> _Scenario:
        try:
            key = keys.for_scenario(scenario)
        except AnswerKeyError as exc:
            return _Scenario(scenario, error=str(exc))
        snapshot_dir = Path(self.config.paths.snapshot_root) / key.incident_id
        try:
            report = loader.load_incident_report(snapshot_dir)
        except IncidentReportError as exc:
            return _Scenario(scenario, key=key, error=str(exc))
        return _Scenario(scenario, key=key, snapshot_dir=snapshot_dir, report=report)

    async def run(self, plan: EvalPlan, out_dir: Path) -> ResultsTable:
        keys = AnswerKeyStore(self.config.paths.answer_key_root, self.config.seed)
        loader = SnapshotLoader(PathGuard(self.config.paths.snapshot_root))
        systems = plan.ordered_systems()
        scores: list[RunScore] = []
        # Sequential: scenario name, then run index, then the Investigator before the Baseline_Agent (Req 26.3).
        for scenario in sorted(plan.scenarios):
            prepared = self._prepare(scenario, keys, loader)
            for run_index in range(1, plan.runs + 1):
                seed = run_seed(self.config.seed, scenario, run_index)
                for system in systems:
                    score = await self._run_one(prepared, RunIdentity(scenario, system, run_index, seed), Path(out_dir))
                    scores.append(score)
                    if self._progress is not None:
                        self._progress(score)
        table = ResultsTable(
            provider=self.config.llm.provider,
            model=self.config.llm.model,
            seed=self.config.seed,
            runs_per_scenario=plan.runs,
            evaluation_date=self._clock.now().date(),
            scenario_count=len(plan.scenarios),
            eval_command=self._eval_command,
            systems=systems,
            rows=tuple(aggregate(scores)),
            runs=tuple(scores),
        )
        write_results(table, Path(out_dir), self._redactor)
        return table

    async def _run_one(self, scenario: _Scenario, run: RunIdentity, out_dir: Path) -> RunScore:
        redact = self._redactor.redact
        if scenario.error is not None or scenario.key is None:
            return failed_run_score(run, scenario.key, redact(scenario.error or "no answer key"), latency_s=0.0)
        key = scenario.key
        scanner = IsolationScanner(key.canary, self.config.paths.answer_key_root)
        started = self._clock.monotonic()
        client = CountingLLMClient(self._client_factory())
        sink = TimedRunOutputSink(run_dir(out_dir, run.scenario, run.system, run.run_index), self._redactor, self._clock)
        handle: RunHandle | None = None
        try:
            inputs = RunInputs(
                snapshot_dir=scenario.snapshot_dir,
                incident_report=scenario.report,
                config=self.config.for_system_under_test(seed=run.seed),
            )
            deps = RunDependencies(llm_client=client, clock=self._clock, redactor=self._redactor)
            handle = self._system_factory(run.system, inputs, deps, sink)
            result = await handle.execute()
            ended = sink.json_written_at if sink.json_written_at is not None else self._clock.monotonic()
            violations = scanner.scan(self._artifacts(client, sink.out_dir))
            return score_run(
                run, result.report, result.ledger, result.trace, key, latency_s=ended - started, isolation_violations=violations
            )
        except Exception as exc:  # an unhandled error fails this Run only (Req 26.7)
            counts = TraceCounts(
                tool_calls=_tool_calls_so_far(handle),
                input_tokens=client.usage.input_tokens,
                output_tokens=client.usage.output_tokens,
            )
            return failed_run_score(
                run,
                key,
                redact(f"{type(exc).__name__}: {exc}")[:2000],
                latency_s=self._clock.monotonic() - started,
                counts=counts,
                isolation_violations=scanner.scan(self._artifacts(client, sink.out_dir)),
            )

    @staticmethod
    def _artifacts(client: CountingLLMClient, directory: Path) -> list[tuple[str, str]]:
        texts = [(f"prompt {i}", prompt) for i, prompt in enumerate(client.prompts, start=1)]
        for name in RUN_FILES:
            path = directory / name
            if path.is_file():
                texts.append((name, path.read_text(encoding="utf-8")))
        return texts


def _tool_calls_so_far(handle: RunHandle | None) -> int:
    """Best effort for a failed Run: both systems keep their usage on ``handle.board``."""
    board = getattr(handle, "board", None)
    usage = getattr(board, "usage", None)
    return int(getattr(usage, "tool_calls", 0) or 0)
