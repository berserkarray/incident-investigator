"""Thin end-to-end slice: generate one scenario, investigate it offline, check every output (task 3.11)."""

import json
from pathlib import Path

import pytest

from incident_investigator.cli import main
from incident_investigator.models.report import InvestigationReport
from incident_investigator.report.writer import load_report

REPO = Path(__file__).resolve().parents[2]
SECTIONS = (
    "## Incident",
    "## Verdict",
    "## Termination",
    "## Hypotheses",
    "## Superseded Hypotheses",
    "## Facts",
    "## Assumptions",
    "## Data Gaps",
    "## Recommended Actions",
    "## Incident Report Claims",
    "## Missing Evidence",
    "## Budget Usage",
    "## Evidence Appendix",
)


def snapshot_bytes(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "scenarios").mkdir()
    spec = REPO / "scenarios" / "bad-config-deploy.json"
    (tmp_path / "scenarios" / spec.name).write_text(spec.read_text())
    (tmp_path / "config.toml").write_text(
        'seed = 42\n[llm]\nprovider = "fake"\nmodel = "offline-responder"\n'
        '[paths]\nsnapshot_root = "snapshots"\nanswer_key_root = "answer_keys"\n'
    )
    return tmp_path


def test_generate_then_investigate_offline(workspace: Path, capsys):
    config = workspace / "config.toml"
    assert main(["generate", "--seed", "42", "--scenarios", str(workspace / "scenarios"), "--config", str(config)]) == 0
    snapshot_root = workspace / "snapshots"
    (incident_dir,) = [p for p in snapshot_root.iterdir() if p.is_dir()]
    (key_file,) = list((workspace / "answer_keys").glob("*.json"))
    canary = json.loads(key_file.read_text())["canary"]
    before = snapshot_bytes(snapshot_root)

    out = workspace / "out"
    code = main(["investigate", "--incident", str(incident_dir), "--system", "multi", "--config", str(config), "--out", str(out)])
    assert code == 0, capsys.readouterr().err

    files = {name: (out / name) for name in ("report.json", "report.md", "trace.json", "ledger.json")}
    assert all(f.is_file() for f in files.values())
    report = load_report(files["report.json"])
    assert isinstance(report, InvestigationReport)
    assert report.system == "multi"
    assert report.incident.incident_id == incident_dir.name
    assert report.hypotheses, "the offline run should produce ranked hypotheses"
    assert [h.rank for h in report.hypotheses] == list(range(1, len(report.hypotheses) + 1))
    assert report.evidence_appendix, "the opening query_logs request should produce evidence"
    assert len(report.claim_statuses) == 4  # two symptoms, affected service, start time

    markdown = files["report.md"].read_text()
    for heading in SECTIONS:
        assert heading in markdown, heading
    for entry in report.evidence_appendix:
        assert entry.evidence_id in markdown
    assert str(report.verdict) in markdown and str(report.termination_reason) in markdown

    for path in files.values():
        assert canary.lower() not in path.read_text().lower(), f"canary leaked into {path.name}"
    assert snapshot_bytes(snapshot_root) == before, "investigate must leave the Snapshot_Root unchanged"

    trace = json.loads(files["trace.json"].read_text())
    transitions = [e for e in trace["events"] if e["type"] == "task_transition"]
    starts = {e["task_id"]: i for i, e in enumerate(transitions) if e["to_state"] == "running"}
    ends = {e["task_id"]: i for i, e in enumerate(transitions) if e["to_state"] in ("completed", "failed")}
    assert starts["T1"] < ends["T1"] and starts["T2"] < ends["T1"], "both initial specialists start before either ends"


def test_invalid_incident_report_exits_before_any_call(workspace: Path, capsys):
    config = workspace / "config.toml"
    assert main(["generate", "--seed", "42", "--scenarios", str(workspace / "scenarios"), "--config", str(config)]) == 0
    (incident_dir,) = [p for p in (workspace / "snapshots").iterdir() if p.is_dir()]
    report_file = incident_dir / "incident_report.json"
    data = json.loads(report_file.read_text())
    data["severity"] = "critical"
    report_file.write_text(json.dumps(data))
    out = workspace / "out"
    code = main(["investigate", "--incident", str(incident_dir), "--system", "multi", "--config", str(config), "--out", str(out)])
    assert code == 1
    assert "severity" in capsys.readouterr().err
    assert not out.exists()
