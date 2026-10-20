"""Evaluator orchestration: Run order, Run Seeds, per-Run outputs, failed Runs, isolation (Req 24.7, 26.1-26.7, 26.10)."""

import json
from pathlib import Path

from factories import make_config, make_snapshot, write_snapshot_dir

from incident_investigator.clock import FakeClock
from incident_investigator.evaluation.isolation import IsolationScanner
from incident_investigator.evaluation.runner import EvalPlan, EvaluationRunner, default_system_factory, run_seed
from incident_investigator.llm.offline import OfflineResponder
from incident_investigator.models.answer_key import AnswerKey
from incident_investigator.models.config import InvestigatorConfig
from incident_investigator.models.results import ResultsTable
from incident_investigator.models.vocab import SystemName

SCENARIO = "zeta-scenario"
MISSING = "omega-scenario"
CANARY = "CANARY-" + "c" * 48


def workspace(tmp_path: Path):
    write_snapshot_dir(tmp_path / "snapshots")
    keys = tmp_path / "keys"
    keys.mkdir()
    key = AnswerKey(
        incident_id=make_snapshot().incident_id,
        scenario_name=SCENARIO,
        seed=42,
        outcome="root_cause",
        root_cause={"component": "checkout-a1b", "cause_type": "config_error"},
        key_evidence=("log-2",),
        canary=CANARY,
    )
    (keys / f"{key.incident_id}.json").write_text(key.model_dump_json())
    return make_config(paths={"snapshot_root": str(tmp_path / "snapshots"), "answer_key_root": str(keys)})


async def test_runs_in_order_with_shared_seeds_and_continue_after_failures(tmp_path):
    config = workspace(tmp_path)
    calls = []

    def factory(system, inputs, deps, sink):
        calls.append((system, inputs))
        if system is SystemName.BASELINE and len(calls) == 2:
            raise RuntimeError("injected defect")
        return default_system_factory(system, inputs, deps, sink)

    runner = EvaluationRunner(config, client_factory=OfflineResponder, clock=FakeClock(), system_factory=factory)
    out = tmp_path / "out"
    table = await runner.run(EvalPlan(scenarios=(SCENARIO, MISSING), runs=2), out)

    # Req 26.1, 26.3: N runs per system and scenario, ordered scenario, index, multi before baseline
    assert [(r.scenario, r.run_index, r.system) for r in table.runs] == [
        (MISSING, 1, "multi"), (MISSING, 1, "baseline"), (MISSING, 2, "multi"), (MISSING, 2, "baseline"),
        (SCENARIO, 1, "multi"), (SCENARIO, 1, "baseline"), (SCENARIO, 2, "multi"), (SCENARIO, 2, "baseline"),
    ]
    # Req 26.2: the pair with the same index shares the Seed, snapshot and Incident_Report
    (m1, i1), (b1, j1), (m2, i2), (b2, j2) = calls
    assert i1.config.seed == j1.config.seed == run_seed(42, SCENARIO, 1) != i2.config.seed == j2.config.seed
    assert i1.snapshot_dir == j1.snapshot_dir and i1.incident_report == j1.incident_report
    # Req 24.7, 26.10: Runs get no Answer_Key_Root, canary or scenario name
    for _, inputs in calls:
        assert isinstance(inputs.config, InvestigatorConfig)
        dumped = inputs.model_dump_json()
        assert str(tmp_path / "keys") not in dumped
        assert CANARY not in dumped and SCENARIO not in dumped

    by_key = {(r.scenario, r.run_index, r.system): r for r in table.runs}
    assert all(r.failed and "no answer key" in r.error for r in table.runs if r.scenario == MISSING)
    injected = by_key[(SCENARIO, 1, "baseline")]
    assert injected.failed and "injected defect" in injected.error and injected.top1 == 0  # Req 26.7
    assert not by_key[(SCENARIO, 2, "baseline")].failed  # the evaluation continued
    for run in table.runs:
        assert run.isolation_violations == ()

    # Req 26.4: per-Run outputs, the Run Seed in the trace, nothing shared between Runs
    for index in (1, 2):
        directory = out / "runs" / SCENARIO / "multi" / f"run-{index}"
        assert {p.name for p in directory.iterdir()} == {"report.json", "report.md", "trace.json", "ledger.json"}
        trace = json.loads((directory / "trace.json").read_text())
        assert trace["events"][0]["seed"] == run_seed(42, SCENARIO, index)
    assert not (out / "runs" / SCENARIO / "baseline" / "run-1" / "report.json").exists()
    assert (out / "runs" / SCENARIO / "baseline" / "run-2" / "report.json").is_file()

    # Req 26.5: the JSON on disk equals the returned table; Markdown sits beside it
    assert ResultsTable.model_validate_json((out / "results.json").read_text()) == table
    assert (out / "results.md").is_file()
    rows = {(r.scenario, r.system): r for r in table.rows}
    assert rows[(SCENARIO, "baseline")].failed_runs == 1 and rows[(MISSING, "multi")].failed_runs == 2


async def test_system_selection_and_run_count(tmp_path):
    config = workspace(tmp_path)
    runner = EvaluationRunner(config, client_factory=OfflineResponder, clock=FakeClock())
    table = await runner.run(EvalPlan(scenarios=(SCENARIO,), systems=(SystemName.BASELINE,), runs=1), tmp_path / "out")
    assert [(r.system, r.run_index) for r in table.runs] == [("baseline", 1)]
    assert table.systems == (SystemName.BASELINE,) and table.runs_per_scenario == 1
    assert not table.runs[0].failed and table.runs[0].key_evidence_recall is not None


def test_isolation_scanner_finds_canary_and_answer_key_root(tmp_path):
    scanner = IsolationScanner(CANARY, tmp_path / "keys")
    texts = [("prompt 1", f"x {CANARY.lower()} y"), ("trace.json", f"path {tmp_path / 'keys'}/k.json"), ("report.md", "clean")]
    assert scanner.scan(texts) == ["canary found in prompt 1", "answer_key_root found in trace.json"]
