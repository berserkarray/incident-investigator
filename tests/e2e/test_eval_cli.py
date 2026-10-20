"""``eval`` and ``investigate --system baseline`` through the CLI, offline (Req 26.6, 26.8, 28.2-28.6)."""

import json
from pathlib import Path

import pytest
from test_thin_slice import SECTIONS, snapshot_bytes, workspace  # noqa: F401  (fixture)

from incident_investigator.cli import main
from incident_investigator.models.results import ResultsTable
from incident_investigator.report.writer import load_report

RUN_FILES = ("report.json", "report.md", "trace.json", "ledger.json")


def generate(workspace: Path) -> Path:
    config = workspace / "config.toml"
    assert main(["generate", "--seed", "42", "--scenarios", str(workspace / "scenarios"), "--config", str(config)]) == 0
    return config


def canary_of(workspace: Path) -> str:
    (key_file,) = (workspace / "answer_keys").glob("*.json")
    return json.loads(key_file.read_text())["canary"].lower()


def test_investigate_baseline_writes_one_report(workspace: Path, capsys):
    config = generate(workspace)
    (incident_dir,) = [p for p in (workspace / "snapshots").iterdir() if p.is_dir()]
    out = workspace / "out-baseline"
    code = main(["investigate", "--incident", str(incident_dir), "--system", "baseline", "--config", str(config), "--out", str(out)])
    assert code == 0, capsys.readouterr().err
    assert all((out / name).is_file() for name in RUN_FILES)
    report = load_report(out / "report.json")
    assert report.system == "baseline" and report.termination_reason in ("final_answer", "baseline_failed")
    markdown = (out / "report.md").read_text()
    assert all(heading in markdown for heading in SECTIONS)
    canary = canary_of(workspace)
    assert all(canary not in (out / name).read_text().lower() for name in RUN_FILES)


def test_eval_runs_both_systems_and_writes_the_results_table(workspace: Path, capsys):
    config = generate(workspace)
    before = snapshot_bytes(workspace / "snapshots")
    out = workspace / "results"
    argv = ["eval", "--config", str(config), "--out", str(out), "--runs", "2", "--scenarios", str(workspace / "scenarios")]
    assert main(argv) == 0, capsys.readouterr().err

    table = ResultsTable.model_validate_json((out / "results.json").read_text())
    assert table.provider == "fake" and table.runs_per_scenario == 2 and table.scenario_count == 1
    assert table.eval_command.startswith("incident-investigator eval --config")
    assert [(r.run_index, r.system) for r in table.runs] == [(1, "multi"), (1, "baseline"), (2, "multi"), (2, "baseline")]
    assert not any(r.failed for r in table.runs) and not any(r.isolation_violations for r in table.runs)
    markdown = (out / "results.md").read_text()
    assert "not measure real LLM accuracy" in markdown and table.eval_command in markdown

    canary = canary_of(workspace)
    seeds = {}
    for run in table.runs:
        directory = out / "runs" / "bad-config-deploy" / run.system / f"run-{run.run_index}"
        assert all((directory / name).is_file() for name in RUN_FILES)
        assert all(canary not in (directory / name).read_text().lower() for name in RUN_FILES)
        trace = json.loads((directory / "trace.json").read_text())
        seeds[(run.system, run.run_index)] = trace["events"][0]["seed"]
    assert seeds[("multi", 1)] == seeds[("baseline", 1)] != seeds[("multi", 2)] == seeds[("baseline", 2)]
    assert snapshot_bytes(workspace / "snapshots") == before  # Req 2.5


def test_eval_selects_scenario_and_system(workspace: Path, capsys):
    config = generate(workspace)
    out = workspace / "results"
    argv = ["eval", "--config", str(config), "--out", str(out), "--runs", "1", "--system", "baseline",
            "--scenario", "bad-config-deploy", "--scenarios", str(workspace / "scenarios")]
    assert main(argv) == 0, capsys.readouterr().err
    table = ResultsTable.model_validate_json((out / "results.json").read_text())
    assert [(r.scenario, r.system) for r in table.runs] == [("bad-config-deploy", "baseline")]


@pytest.mark.parametrize("runs", ["0", "11", "two"])
def test_eval_rejects_a_bad_run_count(workspace: Path, capsys, runs):
    with pytest.raises(SystemExit) as exit_info:
        main(["eval", "--config", str(workspace / "config.toml"), "--out", str(workspace / "o"), "--runs", runs])
    assert exit_info.value.code == 2
    assert "--runs" in capsys.readouterr().err
    assert not (workspace / "o").exists()


def test_eval_rejects_an_unknown_system(workspace: Path, capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["eval", "--config", str(workspace / "config.toml"), "--out", str(workspace / "o"), "--system", "solo"])
    assert exit_info.value.code == 2 and "--system" in capsys.readouterr().err


def test_eval_rejects_an_unknown_scenario_before_any_run(workspace: Path, capsys):
    config = generate(workspace)
    out = workspace / "o"
    argv = ["eval", "--config", str(config), "--out", str(out), "--scenario", "no-such-scenario", "--scenarios", str(workspace / "scenarios")]
    assert main(argv) == 1
    err = capsys.readouterr().err
    assert "--scenario" in err and "no-such-scenario" in err
    assert not out.exists()


def test_eval_requires_the_api_key_for_a_real_provider(workspace: Path, capsys):
    config = workspace / "openai.toml"
    config.write_text(
        'seed = 42\n[llm]\nprovider = "openai"\nmodel = "gpt-5.4-mini"\n'
        '[paths]\nsnapshot_root = "snapshots"\nanswer_key_root = "answer_keys"\n'
    )
    out = workspace / "o"
    assert main(["eval", "--config", str(config), "--out", str(out), "--scenarios", str(workspace / "scenarios")]) == 1
    assert "OPENAI_API_KEY" in capsys.readouterr().err
    assert not out.exists()
