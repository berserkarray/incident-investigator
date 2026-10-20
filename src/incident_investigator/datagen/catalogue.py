"""Scenario_Spec files: load, validate and format (Req 22.1-22.4). Only ``datagen`` reads ``scenarios/``."""

import json
from pathlib import Path

from pydantic import ValidationError

from ..models.scenario import ScenarioSpec


class ScenarioSpecError(Exception):
    def __init__(self, path: Path, problems: list[str]):
        super().__init__(f"invalid scenario spec {path}:\n" + "\n".join(f"  - {p}" for p in problems))
        self.path = path
        self.problems = problems


def load_scenario_spec(path: Path) -> ScenarioSpec:
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ScenarioSpecError(path, [f"<file>: cannot be read or parsed ({exc})"]) from None
    try:
        spec = ScenarioSpec.model_validate(data)
    except ValidationError as exc:
        raise ScenarioSpecError(
            path,
            [f"{'.'.join(str(p) for p in e['loc']) or '<root>'}: {e['msg']}" for e in exc.errors(include_url=False)],
        ) from None
    if spec.name != path.stem:
        raise ScenarioSpecError(path, [f"name: must match the file name ({path.stem!r})"])
    return spec


def format_scenario_spec(spec: ScenarioSpec) -> str:
    return json.dumps(spec.model_dump(mode="json"), sort_keys=True, indent=2) + "\n"


def load_catalogue(directory: Path) -> list[ScenarioSpec]:
    """Every ``*.json`` spec in ``directory``, ordered by name."""
    return [load_scenario_spec(p) for p in sorted(Path(directory).glob("*.json"))]
