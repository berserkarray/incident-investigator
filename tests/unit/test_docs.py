"""Doc lint for the written deliverables (task 10.7; Req 31.1-31.5, 31.7, 31.8, 31.9).

Checks structure and cross-references only: README sections and the single ``mermaid`` fence,
every config field with its default, every Termination_Reason, TECHNICAL_NOTE headings, word
limit and the uniform-penalty sentence, exact dependency pins and existing deliverable paths.
The Mermaid syntax itself is not parsed here (that needs a Mermaid CLI).
"""

import re
import tomllib
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from incident_investigator.models.config import Config
from incident_investigator.models.vocab import TerminationReason

REPO = Path(__file__).resolve().parents[2]
README = (REPO / "README.md").read_text(encoding="utf-8")
NOTE = (REPO / "TECHNICAL_NOTE.md").read_text(encoding="utf-8")
PYPROJECT = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))

README_SECTIONS = (
    "Setup",
    "Architecture",
    "Dataset",
    "Evaluation",
    "Incomplete Functionality",
    "External Dependencies",
    "Deliverables",
)
NOTE_SECTIONS = ("Key Decisions", "Trade-offs", "Limitations", "Potential Improvements")
PENALTY_SENTENCE = (
    "the Confidence_Policy applies one uniform per-Data_Gap penalty to every Hypothesis "
    "regardless of the Data_Gap's relevance to that Hypothesis"
)


def sections(text: str) -> dict[str, str]:
    """``## Heading`` -> body up to the next ``## `` heading (fenced code is kept as is)."""
    parts = re.split(r"^## (.+)$", text, flags=re.MULTILINE)
    return {title.strip(): body for title, body in zip(parts[1::2], parts[2::2], strict=True)}


def leaf_defaults(model: type[BaseModel], prefix: str = "") -> dict[str, Any]:
    """Dotted config field paths with their defaults (``None`` for required fields)."""
    found: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        path = f"{prefix}{name}"
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            found.update(leaf_defaults(annotation, f"{path}."))
        else:
            found[path] = None if field.is_required() else field.default
    return found


def dependency_specs() -> list[str]:
    specs = list(PYPROJECT["project"]["dependencies"])
    for group in PYPROJECT["project"].get("optional-dependencies", {}).values():
        specs.extend(group)
    specs.extend(PYPROJECT["build-system"]["requires"])
    return specs


# README


def test_readme_has_the_required_sections_in_order():
    titles = list(sections(README))
    assert [t for t in titles if t in README_SECTIONS] == list(README_SECTIONS)


def test_readme_has_exactly_one_mermaid_block_in_architecture():
    assert README.count("```mermaid") == 1
    assert "```mermaid" in sections(README)["Architecture"]


def test_architecture_names_the_agents_and_every_termination_reason():
    architecture = sections(README)["Architecture"]
    for name in ("Orchestrator", "Observability_Agent", "Dependency_Agent", "Reasoning_Agent", "Critic_Agent",
                 "Reporting_Agent", "Blackboard", "Tool_Layer", "LLM_Client"):
        assert name in architecture, name
    for reason in TerminationReason:
        assert f"`{reason.value}`" in architecture, reason.value


def test_setup_states_python_install_keys_and_commands():
    setup = sections(README)["Setup"]
    assert "Python 3.11" in setup and 'pip install -e ".[dev]"' in setup
    assert "OPENAI_API_KEY" in setup and "ANTHROPIC_API_KEY" in setup
    for command in (
        "incident-investigator generate --seed 42",
        "incident-investigator investigate --incident data/snapshots/",
        "incident-investigator eval --config",
        "\npytest\n",
    ):
        assert command in setup, command
    assert "--system multi" in setup and "--system baseline" in setup and "--out" in setup


def _default_cell(setup: str, field: str) -> str:
    rows = [line for line in setup.splitlines() if line.startswith(f"| `{field}` |")]
    assert len(rows) == 1, f"config table needs exactly one row for {field}"
    cells = [c.strip() for c in rows[0].strip("|").split("|")]
    return cells[2]


@pytest.mark.parametrize("field,default", sorted(leaf_defaults(Config).items()))
def test_setup_documents_every_config_field_with_its_default(field, default):
    cell = _default_cell(sections(README)["Setup"], field)
    if default is None:
        assert cell.startswith("required"), cell
        return
    first = cell.split()[0].strip("`")
    if isinstance(default, bool):
        assert first == str(default).lower(), cell
    elif isinstance(default, (int, float)):
        assert float(first) == float(default), cell
    else:
        assert first == str(default), cell


def test_dataset_states_the_seed_and_the_generate_command():
    dataset = sections(README)["Dataset"]
    assert "`42`" in dataset and "incident-investigator generate --seed 42 --config config.toml" in dataset


def test_incomplete_functionality_lists_the_anthropic_adapter_and_optional_tasks():
    incomplete = sections(README)["Incomplete Functionality"]
    assert "Req 5.1, 5.2" in incomplete and "Anthropic" in incomplete
    for task in ("1.12", "5.6", "5.7", "5.11", "6.10", "7.9", "7.10", "8.7", "9.8", "9.9"):
        assert task in incomplete, task


def test_external_dependencies_list_every_pinned_package():
    deps = sections(README)["External Dependencies"]
    assert "OPENAI_API_KEY" in deps and "no API key" in deps
    for spec in dependency_specs():
        assert f"`{spec}`" in deps, spec


def test_every_deliverable_path_exists():
    deliverables = sections(README)["Deliverables"]
    paths = re.findall(r"\(([^)\s]+)\)", deliverables)
    assert len(paths) >= 9
    for path in paths:
        assert (REPO / path).exists(), path
    for required in ("transcripts/", "results/results.md", "results/results.json", "scenarios/", "tests/",
                     "TECHNICAL_NOTE.md", "ASSUMPTIONS.md"):
        assert required in paths, required


# TECHNICAL_NOTE.md


def test_technical_note_sections_each_have_an_entry():
    found = sections(NOTE)
    assert list(found) == list(NOTE_SECTIONS)
    for title, body in found.items():
        assert re.search(r"^- \S", body, flags=re.MULTILINE), f"{title} has no entry"


def test_technical_note_is_at_most_500_words():
    assert len(NOTE.split()) <= 500


def test_technical_note_states_the_uniform_penalty_limitation():
    limitations = " ".join(sections(NOTE)["Limitations"].split()).lower()
    assert PENALTY_SENTENCE.lower() in limitations


# Packaging and assumptions


def test_every_dependency_is_pinned_exactly():
    specs = dependency_specs()
    assert specs
    for spec in specs:
        assert re.fullmatch(r"[A-Za-z0-9_.\-]+(\[[A-Za-z0-9_,\-]+\])?==[0-9][0-9A-Za-z.\-+]*", spec), spec


def test_assumptions_lists_19_numbered_assumptions():
    text = (REPO / "ASSUMPTIONS.md").read_text(encoding="utf-8")
    numbers = [int(n) for n in re.findall(r"^(\d+)\. ", text, flags=re.MULTILINE)]
    assert numbers == list(range(1, 20))
