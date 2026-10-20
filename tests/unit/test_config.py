"""Configuration loading and validation (Req 7.6, 18.1, 24.7, 29.1, 29.2)."""

from pathlib import Path

import pytest
from factories import make_config

from incident_investigator.models.config import ConfigError, load_config

REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("name", ["config.toml", "config.fake.toml"])
def test_shipped_configs_load(name):
    config = load_config(REPO / name)
    assert config.orchestrator.concurrency_limit == 4
    assert config.paths.snapshot_root == REPO / "data/snapshots"


def test_real_config_uses_openai_gpt_5_4_mini():
    config = load_config(REPO / "config.toml")
    assert (config.llm.provider, config.llm.model) == ("openai", "gpt-5.4-mini")


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_absent_fields_get_defaults(tmp_path):
    config = load_config(write(tmp_path, '[llm]\nprovider = "fake"\nmodel = "m"\n'))
    assert config.budgets.critic_rounds == 3
    assert config.budgets.tokens == 400_000
    assert config.orchestrator.change_lookback_s == 604_800
    assert config.report.observation_summary_size == 5
    assert config.seed == 42


def test_every_problem_is_named(tmp_path):
    text = """
seed = 1
colour = "blue"

[llm]
provider = "fake"

[orchestrator]
concurrency_limit = 17

[budgets]
critic_rounds = 4
"""
    with pytest.raises(ConfigError) as info:
        load_config(write(tmp_path, text))
    fields = set(info.value.fields)
    assert {"colour", "llm.model", "orchestrator.concurrency_limit", "budgets.critic_rounds"} <= fields
    message = str(info.value)
    assert "unknown field" in message and "missing required field" in message


@pytest.mark.parametrize(
    "sections",
    [
        {"budgets": {"tokens": 100, "reporting_reserve_tokens": 100}},
        {"tools": {"failure_injection": {"timeout_p": 0.6, "partial_p": 0.5}}},
        {"tools": {"retry": {"base_delay_s": 5.0, "max_delay_s": 1.0}}},
        {"paths": {"snapshot_root": "data", "answer_key_root": "data/answer_keys"}},
    ],
)
def test_cross_field_rules(sections):
    with pytest.raises(ConfigError):
        make_config(**sections)


def test_unreadable_and_unparseable_files_name_the_file(tmp_path):
    with pytest.raises(ConfigError, match="cannot be read"):
        load_config(tmp_path / "missing.toml")
    with pytest.raises(ConfigError, match="cannot be parsed"):
        load_config(write(tmp_path, "[llm\nprovider ="))


def test_run_config_has_no_answer_key_path():
    run_config = make_config(seed=7).for_system_under_test(seed=99)
    assert run_config.seed == 99
    dumped = run_config.model_dump_json()
    assert "answer_key" not in dumped
