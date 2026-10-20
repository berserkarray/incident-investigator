"""Data_Generator and Scenario_Catalogue (Req 22.2, 22.5, 22.6, 22.10, 22.14, 22.16, 22.17, 23.1-23.16, 24.5, 24.11, 28.8)."""

import json
from pathlib import Path

import pytest

from incident_investigator.cli import main
from incident_investigator.datagen.catalogue import ScenarioSpecError, format_scenario_spec, load_catalogue, load_scenario_spec
from incident_investigator.datagen.checks import CONDITION_CHECKS, condition_checks, generic_checks, leak_scan
from incident_investigator.datagen.generator import GenerationError, generate, write_incident
from incident_investigator.models.scenario import CatalogueCondition, ScenarioSpec
from incident_investigator.models.snapshot import LogRecord
from incident_investigator.models.vocab import CauseType, LogLevel, ToolName
from incident_investigator.tools.snapshot import PathGuard, SnapshotLoader

REPO = Path(__file__).resolve().parents[2]
SCENARIOS = REPO / "scenarios"
SEED = 42
UNUSED_CAUSES = {
    CauseType.DNS_FAILURE,
    CauseType.RATE_LIMITING,
    CauseType.CODE_DEFECT,
    CauseType.DEPENDENCY_TIMEOUT,
    CauseType.CAPACITY_LIMIT,
    CauseType.DATA_CORRUPTION,
    CauseType.CLOCK_SKEW,
}


@pytest.fixture(scope="module")
def catalogue() -> list[ScenarioSpec]:
    return load_catalogue(SCENARIOS)


@pytest.fixture(scope="module")
def generated(catalogue):
    return {spec.name: generate(spec, SEED) for spec in catalogue}


def snapshot_tree(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


# catalogue (Req 23.13, 23.14)


def test_catalogue_has_twelve_scenarios_covering_twelve_distinct_conditions(catalogue):
    assert len(catalogue) == 12 and len({s.name for s in catalogue}) == 12
    conditions = [c for spec in catalogue for c in spec.conditions]
    assert all(len(spec.conditions) == 1 for spec in catalogue)
    assert sorted(conditions) == sorted(CatalogueCondition) and set(CONDITION_CHECKS) == set(CatalogueCondition)


def test_catalogue_specs_round_trip(catalogue, tmp_path):
    """Req 22.3, 22.4"""
    for spec in catalogue:
        path = tmp_path / f"{spec.name}.json"
        path.write_text(format_scenario_spec(spec))
        assert load_scenario_spec(path) == spec


# generation (Req 22.5, 22.13, 23.1-23.12, 23.15)


def test_every_scenario_generates_and_passes_its_checks(generated):
    assert len(generated) == 12
    for name, g in generated.items():
        assert generic_checks(g) == [], name
        assert leak_scan(g.snapshot_files(), g) == [], name
        assert condition_checks(g) == [], name
        key = g.answer_key
        if name == "insufficient-evidence":
            assert key.outcome == "insufficient_evidence" and key.root_cause is None and not key.key_evidence
        else:
            past = {p.record_id for p in g.snapshot.past_incidents}
            assert key.root_cause is not None and set(key.key_evidence) - past, name


def test_red_herrings_are_present_and_never_key_evidence(generated):
    """Req 22.10"""
    for g in generated.values():
        declared = g.spec.trap.red_herrings if g.spec.trap else ()
        assert len(g.herring_record_ids) == len(declared)
        for ids in g.herring_record_ids:
            assert ids and not set(ids) & set(g.answer_key.key_evidence)


def test_answer_keys_never_use_the_unused_cause_types(generated):
    for g in generated.values():
        used = set(g.answer_key.decoy_cause_types)
        if g.answer_key.root_cause is not None:
            used.add(g.answer_key.root_cause.cause_type)
        assert not used & UNUSED_CAUSES, g.spec.name


def test_logs_timeout_expects_a_query_logs_timeout_gap(generated):
    """Req 23.9"""
    key = generated["logs-timeout"].answer_key
    assert [(str(g.source), g.reason) for g in key.expected_data_gaps] == [(ToolName.QUERY_LOGS.value, "timeout")]
    assert all(not g.answer_key.expected_data_gaps for name, g in generated.items() if name != "logs-timeout")


def test_ids_and_canaries_are_opaque_and_unique(generated):
    """Req 22.11, 22.16, 24.5"""
    incident_ids = [g.snapshot.incident_id for g in generated.values()]
    canaries = [g.answer_key.canary for g in generated.values()]
    assert len(set(incident_ids)) == 12 and len(set(canaries)) == 12
    assert all(len(c) >= 32 for c in canaries)
    for g in generated.values():
        assert g.spec.name not in g.snapshot.incident_id and g.spec.fault.template.value not in g.snapshot.incident_id
        for text in g.snapshot_files().values():
            lowered = text.lower()
            assert all(c.lower() not in lowered for c in canaries)
    other_seed = generate(load_scenario_spec(SCENARIOS / "bad-config-deploy.json"), SEED + 1)
    assert other_seed.snapshot.incident_id != generated["bad-config-deploy"].snapshot.incident_id


# determinism and round trip (Req 22.6, 22.14)


def test_two_generations_are_byte_identical(catalogue, generated, tmp_path):
    for spec in catalogue:
        again = generate(spec, SEED)
        assert again.snapshot_files() == generated[spec.name].snapshot_files(), spec.name
        assert again.answer_key_file() == generated[spec.name].answer_key_file(), spec.name
    first, second = tmp_path / "a", tmp_path / "b"
    for root in (first, second):
        for spec in catalogue[:3]:
            write_incident(generate(spec, SEED), root / "snapshots", root / "keys")
    assert snapshot_tree(first) == snapshot_tree(second)


def test_snapshot_round_trip(generated, tmp_path):
    g = generated["cache-stampede"]
    target = write_incident(g, tmp_path / "snapshots", tmp_path / "keys")
    loader = SnapshotLoader(PathGuard(tmp_path / "snapshots"))
    assert loader.load(target) == g.snapshot
    assert loader.load_incident_report(target) == g.report


# rejection (Req 22.2, 22.17, 23.16, 28.8)


def workspace(tmp_path: Path, specs: dict[str, dict]) -> Path:
    (tmp_path / "scenarios").mkdir()
    for name, data in specs.items():
        (tmp_path / "scenarios" / f"{name}.json").write_text(json.dumps(data))
    (tmp_path / "config.toml").write_text(
        '[llm]\nprovider = "fake"\nmodel = "offline-responder"\n[paths]\nsnapshot_root = "snapshots"\nanswer_key_root = "keys"\n'
    )
    return tmp_path


def run_generate(root: Path) -> int:
    return main(["generate", "--seed", str(SEED), "--scenarios", str(root / "scenarios"), "--config", str(root / "config.toml")])


def spec_data(name: str) -> dict:
    return json.loads((SCENARIOS / f"{name}.json").read_text())


def test_invalid_spec_is_rejected_with_field_names_and_nothing_is_written(tmp_path, capsys):
    broken = spec_data("bad-config-deploy") | {"name": "broken", "service_count": 20}
    broken["fault"] = broken["fault"] | {"cause_type": "gremlins", "template": "magic"}
    broken["failure_profile"] = {
        "forced": [{"tool": "get_dependencies", "failure_type": "stale_data"}, {"tool": "query_traces", "failure_type": "timeout"}]
    }
    root = workspace(tmp_path, {"broken": broken, "database-failover": spec_data("database-failover")})
    assert run_generate(root) == 1
    err = capsys.readouterr().err
    for field in ("service_count", "fault.template", "fault.cause_type", "failure_profile.forced"):
        assert field in err, field
    assert not (root / "snapshots").exists() and not (root / "keys").exists()

    with pytest.raises(ScenarioSpecError) as info:
        load_scenario_spec(root / "scenarios" / "broken.json")
    assert any(p.startswith("failure_profile.forced.1.tool") for p in info.value.problems)


def test_failure_profile_rules_are_enforced_on_specs(tmp_path):
    """Req 14.9, 22.2: no stale-data for get_dependencies, at most one forced failure per Tool."""
    data = spec_data("bad-config-deploy")
    data["failure_profile"] = {
        "forced": [
            {"tool": "get_dependencies", "failure_type": "stale_data"},
            {"tool": "query_logs", "failure_type": "timeout"},
            {"tool": "query_logs", "failure_type": "partial_result"},
        ]
    }
    path = tmp_path / "bad-config-deploy.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ScenarioSpecError) as info:
        load_scenario_spec(path)
    (problem,) = info.value.problems
    assert problem.startswith("failure_profile")
    assert "stale_data is not allowed for get_dependencies" in problem and "more than one forced failure for query_logs" in problem


def test_condition_violation_names_scenario_seed_and_condition_and_writes_nothing(tmp_path, capsys):
    spec = spec_data("database-failover") | {"trap": None}
    root = workspace(tmp_path, {"database-failover": spec})
    assert run_generate(root) == 1
    err = capsys.readouterr().err
    assert "database-failover" in err and f"seed {SEED}" in err and "db_failover_decoy_deploy" in err
    assert not any((root / "snapshots").glob("inc-*")) and not any((root / "keys").glob("*.json"))


def test_generate_stops_at_the_first_failing_scenario(tmp_path):
    specs = {
        "a-good": spec_data("bad-config-deploy") | {"name": "a-good"},
        "b-bad": spec_data("database-failover") | {"name": "b-bad", "trap": None},
        "c-never": spec_data("disk-full") | {"name": "c-never"},
    }
    root = workspace(tmp_path, specs)
    assert run_generate(root) == 1
    assert len(list((root / "snapshots").glob("inc-*"))) == 1 and len(list((root / "keys").glob("*.json"))) == 1


def test_red_herring_on_the_root_component_is_rejected():
    data = spec_data("slow-memory-leak")
    data["trap"]["red_herrings"][0]["target_role"] = "upstream"
    with pytest.raises(GenerationError) as info:
        generate(ScenarioSpec.model_validate(data), SEED)
    assert any("target_role" in p for p in info.value.problems)


def test_unknown_template_param_is_rejected():
    data = spec_data("disk-full")
    data["fault"]["params"] = {"fill_minutes": 150, "colour": "red"}
    with pytest.raises(GenerationError) as info:
        generate(ScenarioSpec.model_validate(data), SEED)
    assert any("fault.params.colour" in p for p in info.value.problems)


def test_leak_scan_catches_markers_and_names(generated):
    g = generated["bad-config-deploy"]
    files = g.snapshot_files()
    leaky = {name: text for name, text in files.items()}
    leaky["snapshot.json"] += "\nthis log is a red herring for bad-config-deploy\n"
    problems = leak_scan(leaky, g)
    assert any("'red herring'" in p for p in problems) and any("'bad-config-deploy'" in p for p in problems)
    assert leak_scan({"x": "snmptrapd restarted; traps_total=3"}, g) == []


def test_flag_condition_detects_an_uncohorted_error():
    data = spec_data("feature-flag-rollout")
    g = generate(ScenarioSpec.model_validate(data), SEED)
    root = g.answer_key.root_cause.component
    stray = LogRecord(record_id="log-stray", timestamp=g.onset, service=root, level=LogLevel.ERROR, message="boom")
    g.snapshot = g.snapshot.model_copy(update={"logs": (*g.snapshot.logs, stray)})
    assert any("lack cohort" in p for p in CONDITION_CHECKS[CatalogueCondition.FLAG_COHORT_ERRORS](g))
