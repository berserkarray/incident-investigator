"""Static answer-key isolation checks (task 9.7; Req 24.3, 24.4, 24.8, 24.9, 24.10).

An AST scan enforces the import boundaries from the design ("Package layout and import rules"),
a string scan keeps scenario, template and root-Component names out of investigator code and
Prompt_Templates, and basic path checks cover the snapshot path guard and root disjointness.
"""

import ast
import json
import os
from dataclasses import replace
from functools import cache
from pathlib import Path

import pytest
from factories import make_incident_report, make_snapshot
from harness import CATALOGUE, REPO, SCENARIOS_DIR, MixedFakeLLM, generated_incident, run_system, write_incident_dir

from incident_investigator.agents.prompts import templates
from incident_investigator.cli import main
from incident_investigator.clock import FakeClock
from incident_investigator.models.scenario import FaultTemplateName, TrapKind
from incident_investigator.models.vocab import CauseType
from incident_investigator.tools.snapshot import (
    INCIDENT_REPORT_FILE,
    SNAPSHOT_FILE,
    IncidentReportError,
    PathGuard,
    SnapshotLoader,
    SnapshotLoadError,
)

PKG = "incident_investigator"
SRC = REPO / "src" / PKG

INVESTIGATOR_PACKAGES = ("tools", "state", "orchestrator", "agents", "baseline", "llm", "report")
INVESTIGATOR_MODULES = ("runtime", "tracing")  # top-level modules on both systems' run path
BANNED_FOR_INVESTIGATOR = (f"{PKG}.datagen", f"{PKG}.evaluation", f"{PKG}.models.answer_key", f"{PKG}.models.scenario")


# Import scanning


def module_name(path: Path) -> str:
    parts = list(path.relative_to(SRC.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


@cache
def modules() -> dict[str, Path]:
    return {module_name(p): p for p in sorted(SRC.rglob("*.py")) if "__pycache__" not in p.parts}


@cache
def tree(name: str) -> ast.Module:
    path = modules()[name]
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imports_of(name: str) -> frozenset[str]:
    """Every module an import statement anywhere in ``name`` names, including function-level imports.

    ``from X import y`` yields both ``X`` and ``X.y`` because ``y`` may itself be a module.
    """
    is_package = modules()[name].name == "__init__.py"
    package = name.split(".") if is_package else name.split(".")[:-1]
    found: set[str] = set()
    for node in ast.walk(tree(name)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base_parts = package[: len(package) - node.level + 1]
                base = ".".join([*base_parts, *([node.module] if node.module else [])])
            else:
                base = node.module or ""
            found.add(base)
            found.update(f"{base}.{alias.name}" for alias in node.names if alias.name != "*")
    return frozenset(found)


def within(module: str, prefix: str) -> bool:
    return module == prefix or module.startswith(prefix + ".")


def investigator_modules() -> list[str]:
    roots = [f"{PKG}.{p}" for p in INVESTIGATOR_PACKAGES] + [f"{PKG}.{m}" for m in INVESTIGATOR_MODULES]
    return [m for m in modules() if any(within(m, r) for r in roots)]


def offenders(names, banned) -> list[str]:
    return sorted(f"{m} imports {i}" for m in names for i in imports_of(m) if any(within(i, b) for b in banned))


def test_scanner_resolves_relative_and_function_level_imports():
    cli = imports_of(f"{PKG}.cli")
    assert f"{PKG}.datagen.catalogue" in cli  # imported inside cmd_generate
    assert f"{PKG}.orchestrator.run" in cli  # imported inside cmd_investigate
    assert f"{PKG}.models.vocab" in imports_of(f"{PKG}.state.verdict")  # from ..models.vocab import ...
    assert f"{PKG}.datagen.rng" in imports_of(f"{PKG}.datagen.generator")  # from . import rng as ids
    assert len(investigator_modules()) >= 30


def test_only_the_cli_imports_the_evaluator():
    others = [m for m in modules() if m != f"{PKG}.cli" and not within(m, f"{PKG}.evaluation")]
    assert offenders(others, (f"{PKG}.evaluation",)) == []


def test_investigator_imports_no_generator_evaluator_answer_key_or_scenario_code():
    assert offenders(investigator_modules(), BANNED_FOR_INVESTIGATOR) == []


def test_offline_responder_and_fake_llm_import_neither_datagen_nor_evaluation():
    llm_fakes = [f"{PKG}.llm.offline", f"{PKG}.llm.fake"]
    assert offenders(llm_fakes, (f"{PKG}.datagen", f"{PKG}.evaluation", f"{PKG}.models.answer_key")) == []


def test_models_import_nothing_but_models():
    models = [m for m in modules() if within(m, f"{PKG}.models")]
    project = [f"{PKG}.{p.name}" for p in SRC.iterdir() if p.name != "models" and not p.name.startswith("__")]
    assert offenders(models, [p.removesuffix(".py") for p in project]) == []


def test_investigator_code_has_no_dynamic_imports():
    """The import scan would miss ``importlib`` or ``__import__``, so investigator code uses neither."""
    found = set(offenders(investigator_modules(), ("importlib",)))
    for name in investigator_modules():
        found.update(
            f"{name} calls __import__"
            for node in ast.walk(tree(name))
            if isinstance(node, ast.Name) and node.id == "__import__"
        )
    assert sorted(found) == []


# Names in investigator code and Prompt_Templates (Req 24.8)


def string_constants(name: str) -> list[str]:
    return [n.value for n in ast.walk(tree(name)) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


@cache
def forbidden_names() -> dict[str, str]:
    """Scenario names, template names and the root Component of every Answer_Key this branch can generate."""
    names = {s: "scenario name" for s in (*CATALOGUE, *(p.stem for p in SCENARIOS_DIR.glob("*.json")))}
    cause_types = {c.value for c in CauseType}
    # Template names that double as Cause_Type values are the shared cause vocabulary, not a scenario marker.
    names.update({t.value: "fault template name" for t in FaultTemplateName if t.value not in cause_types})
    names.update({t.value: "trap template name" for t in TrapKind})
    return names


def root_components(tmp_path: Path) -> dict[str, str]:
    found = {}
    for spec in sorted(SCENARIOS_DIR.glob("*.json")):
        key = generated_incident(tmp_path / spec.stem, spec.stem, 42).answer_key
        if key.root_cause is not None:
            found[key.root_cause.component] = f"root Component of {spec.stem}"
    return found


def test_investigator_strings_and_templates_name_no_scenario_template_or_root_component(tmp_path):
    terms = {**forbidden_names(), **root_components(tmp_path)}
    assert "bad-config-deploy" in terms and "config_regression" in terms and len(terms) > 20
    texts = [(m, s) for m in investigator_modules() for s in string_constants(m)]
    texts += [("agents.prompts.templates", v) for k, v in vars(templates).items() if isinstance(v, str) and not k.startswith("__")]
    assert len(texts) > 100
    hits = sorted({f"{where}: {label} {term!r}" for where, text in texts for term, label in terms.items() if term.lower() in text.lower()})
    assert hits == []


# Path guard (Req 24.3)


@pytest.fixture
def layout(tmp_path):
    """A Snapshot_Root and, beside it, a valid incident directory outside the root."""
    root = tmp_path / "data"
    inside = write_incident_dir(root, make_snapshot(), make_incident_report())
    outside = write_incident_dir(tmp_path / "elsewhere", make_snapshot(incident_id="inc-outside01"), make_incident_report())
    return inside, outside.snapshot_dir


def spy_reads(monkeypatch) -> list[Path]:
    reads: list[Path] = []
    for method in ("read_bytes", "read_text"):
        original = getattr(Path, method)

        def spy(self, *args, _original=original, **kwargs):
            reads.append(Path(os.path.realpath(self)))
            return _original(self, *args, **kwargs)

        monkeypatch.setattr(Path, method, spy)
    return reads


@pytest.mark.parametrize("how", ["absolute", "dotdot", "symlink"])
def test_snapshot_outside_the_root_is_rejected_without_reading(layout, monkeypatch, how):
    inside, outside_dir = layout
    root = inside.snapshot_root
    target = {
        "absolute": outside_dir,
        "dotdot": root / ".." / ".." / "elsewhere" / "snapshots" / outside_dir.name,
        "symlink": root / "linked-incident",
    }[how]
    if how == "symlink":
        target.symlink_to(outside_dir, target_is_directory=True)
    loader = SnapshotLoader(PathGuard(root))
    reads = spy_reads(monkeypatch)

    with pytest.raises(SnapshotLoadError) as snapshot_error:
        loader.load(target)
    with pytest.raises(IncidentReportError) as report_error:
        loader.load_incident_report(target)

    assert str(target / SNAPSHOT_FILE) in str(snapshot_error.value)  # names the rejected path
    assert str(target / INCIDENT_REPORT_FILE) in str(report_error.value)
    outside = Path(os.path.realpath(outside_dir))
    assert not [r for r in reads if r.is_relative_to(outside)], "no bytes may be read outside the Snapshot_Root"
    loader.load(inside.snapshot_dir)
    assert reads, "the read spy must observe reads inside the root"


def test_snapshot_inside_the_root_loads(layout):
    inside, _ = layout
    loader = SnapshotLoader(PathGuard(inside.snapshot_root))
    assert loader.load(inside.snapshot_dir).incident_id == "inc-test0001"
    assert loader.load_incident_report(inside.snapshot_dir) == inside.report


async def test_a_run_on_a_snapshot_outside_the_root_makes_no_call(layout, tmp_path):
    inside, outside_dir = layout
    escaped = replace(inside, snapshot_dir=outside_dir)
    llm = MixedFakeLLM([], FakeClock(), fallback=False)
    with pytest.raises(SnapshotLoadError):
        await run_system("multi", escaped, llm, tmp_path / "out")
    assert llm.prompts == []
    assert not (tmp_path / "out").exists()


# Root disjointness in the CLI (Req 24.10, 28.5)


def write_config(directory: Path, snapshot_root: str, answer_key_root: str) -> Path:
    path = directory / "config.toml"
    path.write_text(
        'seed = 42\n[llm]\nprovider = "fake"\nmodel = "offline-responder"\n'
        f"[paths]\nsnapshot_root = {json.dumps(snapshot_root)}\nanswer_key_root = {json.dumps(answer_key_root)}\n",
        encoding="utf-8",
    )
    return path


def investigate(config: Path, incident: Path, out: Path) -> int:
    return main(["investigate", "--incident", str(incident), "--system", "multi", "--config", str(config), "--out", str(out)])


def test_cli_rejects_nested_roots_in_the_config(layout, tmp_path, capsys):
    inside, _ = layout
    config = write_config(inside.root, "snapshots", "snapshots/keys")
    out = tmp_path / "out"
    assert investigate(config, inside.snapshot_dir, out) == 1
    err = capsys.readouterr().err
    assert "snapshot_root" in err and "answer_key_root" in err
    assert not out.exists()


def test_cli_rejects_roots_that_overlap_through_a_symlink(layout, tmp_path, capsys):
    inside, _ = layout
    (inside.snapshot_root / "keys").mkdir()
    (inside.root / "keys-link").symlink_to(inside.snapshot_root / "keys", target_is_directory=True)
    config = write_config(inside.root, "snapshots", "keys-link")
    out = tmp_path / "out"
    assert investigate(config, inside.snapshot_dir, out) == 1
    err = capsys.readouterr().err
    assert str(inside.snapshot_root) in err and "keys-link" in err  # names both roots
    assert not out.exists()


def test_generate_rejects_overlapping_roots_before_writing(tmp_path, capsys):
    snapshots = tmp_path / "snapshots"
    keys = snapshots / "keys"
    code = main(
        ["generate", "--seed", "42", "--scenarios", str(SCENARIOS_DIR), "--snapshot-root", str(snapshots), "--answer-key-root", str(keys)]
    )
    assert code == 1
    err = capsys.readouterr().err
    assert str(snapshots) in err and str(keys) in err
    assert not snapshots.exists()


def test_cli_rejects_an_incident_outside_the_snapshot_root(layout, tmp_path, capsys):
    inside, outside_dir = layout
    config = write_config(inside.root, "snapshots", "answer_keys")
    out = tmp_path / "out"
    assert investigate(config, outside_dir, out) == 1
    assert "--incident" in capsys.readouterr().err
    assert not out.exists()


def test_cli_rejects_an_output_directory_inside_the_snapshot_root(layout, capsys):
    inside, _ = layout
    config = write_config(inside.root, "snapshots", "answer_keys")
    out = inside.snapshot_root / "out"
    assert investigate(config, inside.snapshot_dir, out) == 1
    assert "--out" in capsys.readouterr().err
    assert not out.exists()
