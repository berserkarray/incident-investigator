"""Configuration file model and loader (Req 7.6, 18.1, 24.7, 29.1, 29.2).

Durations are in seconds. Every field except ``llm.provider`` and ``llm.model`` has a default.
Unknown fields are rejected so a typo never falls back silently to a default.
"""

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, ValidationError, model_validator

from .base import NonBlank200, StrictModel
from .budget import BudgetLimits


class LLMSettings(StrictModel):
    provider: Literal["anthropic", "openai", "fake"]
    model: NonBlank200
    request_timeout_s: float = Field(60.0, gt=0)
    max_attempts: int = Field(3, ge=1)
    base_delay_s: float = Field(1.0, gt=0)
    max_delay_s: float = Field(20.0, gt=0)

    @model_validator(mode="after")
    def _delay_order(self) -> "LLMSettings":
        if self.max_delay_s < self.base_delay_s:
            raise ValueError("max_delay_s must be at least base_delay_s")
        return self


class RetrySettings(StrictModel):
    max_attempts: int = Field(3, ge=1, le=10)
    base_delay_s: float = Field(0.5, ge=0.01, le=10)
    max_delay_s: float = Field(8.0, le=60)

    @model_validator(mode="after")
    def _delay_order(self) -> "RetrySettings":
        if self.max_delay_s < self.base_delay_s:
            raise ValueError("max_delay_s must be at least base_delay_s")
        return self


class CircuitBreakerSettings(StrictModel):
    failure_threshold: int = Field(3, ge=1, le=20)
    cooldown_s: float = Field(30.0, ge=0.1, le=600)


class FailureInjectionSettings(StrictModel):
    random_enabled: bool = True
    timeout_p: float = Field(0.05, ge=0.0, le=1.0)
    partial_p: float = Field(0.05, ge=0.0, le=1.0)
    stale_p: float = Field(0.05, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _sum_at_most_one(self) -> "FailureInjectionSettings":
        if self.timeout_p + self.partial_p + self.stale_p > 1.0 + 1e-9:
            raise ValueError("timeout_p + partial_p + stale_p must be at most 1.0")
        return self


class ToolSettings(StrictModel):
    retry: RetrySettings = RetrySettings()
    circuit_breaker: CircuitBreakerSettings = CircuitBreakerSettings()
    failure_injection: FailureInjectionSettings = FailureInjectionSettings()


class OrchestratorSettings(StrictModel):
    concurrency_limit: int = Field(4, ge=1, le=16)
    per_task_step_limit: int = Field(6, ge=2)
    per_task_timeout_s: float = Field(180.0, gt=0)
    pre_incident_margin_s: float = Field(3600.0, ge=0)
    change_lookback_s: float = Field(604800.0, gt=0)
    max_initial_tasks_per_specialist: int = Field(2, ge=1)
    max_evidence_requests_per_round: int = Field(4, ge=1, le=10)


class BudgetSettings(StrictModel):
    critic_rounds: int = Field(3, ge=1, le=3)
    tool_calls: int = Field(60, ge=1)
    tokens: int = Field(400_000, ge=1)
    wall_clock_timeout_s: float = Field(900.0, gt=0)
    reporting_reserve_tokens: int = Field(20_000, ge=0)

    @model_validator(mode="after")
    def _reserve_below_tokens(self) -> "BudgetSettings":
        if self.reporting_reserve_tokens > self.tokens - 1:
            raise ValueError("reporting_reserve_tokens must be at most tokens - 1")
        return self

    def limits(self) -> BudgetLimits:
        return BudgetLimits(
            critic_rounds=self.critic_rounds,
            tool_calls=self.tool_calls,
            tokens=self.tokens,
            wall_clock_s=self.wall_clock_timeout_s,
            reporting_reserve=self.reporting_reserve_tokens,
        )


class PolicySettings(StrictModel):
    data_gap_penalty: float = Field(0.05, ge=0.0, le=1.0)
    conclusive_threshold: float = Field(0.6, ge=0.0, le=1.0)


class ReportSettings(StrictModel):
    max_recommended_actions: int = Field(6, ge=1)
    observation_summary_size: int = Field(5, ge=1, le=20)


def _inside_or_equal(child: Path, parent: Path) -> bool:
    child_norm = Path(os.path.normpath(child))
    parent_norm = Path(os.path.normpath(parent))
    return child_norm == parent_norm or child_norm.is_relative_to(parent_norm)


class PathSettings(StrictModel):
    snapshot_root: Path = Path("data/snapshots")
    answer_key_root: Path = Path("data/answer_keys")

    @model_validator(mode="after")
    def _disjoint(self) -> "PathSettings":
        # Lexical check here; the CLI repeats it on resolved paths (Req 24.10).
        if _inside_or_equal(self.answer_key_root, self.snapshot_root) or _inside_or_equal(
            self.snapshot_root, self.answer_key_root
        ):
            raise ValueError("answer_key_root and snapshot_root must not be equal or nested")
        return self


class InvestigatorConfig(StrictModel):
    """What a Run receives and records. It has no answer-key path field (Req 24.7)."""

    llm: LLMSettings
    tools: ToolSettings
    orchestrator: OrchestratorSettings
    budgets: BudgetSettings
    policy: PolicySettings
    report: ReportSettings
    seed: int
    snapshot_root: Path


class Config(StrictModel):
    llm: LLMSettings
    tools: ToolSettings = ToolSettings()
    orchestrator: OrchestratorSettings = OrchestratorSettings()
    budgets: BudgetSettings = BudgetSettings()
    policy: PolicySettings = PolicySettings()
    report: ReportSettings = ReportSettings()
    seed: int = 42
    paths: PathSettings = PathSettings()

    def for_system_under_test(self, seed: int | None = None) -> InvestigatorConfig:
        """Project the config for a Run: drops the Answer_Key_Root path; optionally overrides the Seed."""
        return InvestigatorConfig(
            llm=self.llm,
            tools=self.tools,
            orchestrator=self.orchestrator,
            budgets=self.budgets,
            policy=self.policy,
            report=self.report,
            seed=self.seed if seed is None else seed,
            snapshot_root=self.paths.snapshot_root,
        )


@dataclass(frozen=True)
class ConfigIssue:
    field: str
    problem: str

    def __str__(self) -> str:
        return f"{self.field}: {self.problem}"


class ConfigError(Exception):
    """Every invalid, unknown or missing field, or an unreadable file (Req 29.2)."""

    def __init__(self, source: Path, issues: tuple[ConfigIssue, ...] = (), reason: str | None = None):
        self.source = source
        self.issues = issues
        self.reason = reason
        if reason is not None:
            message = f"config file {source}: {reason}"
        else:
            lines = "\n".join(f"  - {issue}" for issue in issues)
            message = f"config file {source} has {len(issues)} problem(s):\n{lines}"
        super().__init__(message)

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(issue.field for issue in self.issues)


def config_issues(error: ValidationError) -> tuple[ConfigIssue, ...]:
    issues = []
    for item in error.errors(include_url=False):
        field = ".".join(str(part) for part in item["loc"]) or "<root>"
        if item["type"] == "extra_forbidden":
            problem = "unknown field"
        elif item["type"] == "missing":
            problem = "missing required field"
        else:
            problem = item["msg"]
        issues.append(ConfigIssue(field, problem))
    return tuple(issues)


def parse_config(data: dict, source: Path, base_dir: Path | None = None) -> Config:
    """Validate a config mapping. Relative paths resolve against ``base_dir`` when given."""
    try:
        config = Config.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(source, config_issues(exc)) from None
    if base_dir is not None:
        paths = config.paths
        resolved = PathSettings(
            snapshot_root=paths.snapshot_root if paths.snapshot_root.is_absolute() else base_dir / paths.snapshot_root,
            answer_key_root=paths.answer_key_root
            if paths.answer_key_root.is_absolute()
            else base_dir / paths.answer_key_root,
        )
        config = config.model_copy(update={"paths": resolved})
    return config


def load_config(path: Path) -> Config:
    """Load a TOML config file. Relative paths resolve against the file's directory."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise ConfigError(path, reason=f"cannot be read ({exc.strerror or exc})") from None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(path, reason=f"cannot be parsed as TOML ({exc})") from None
    return parse_config(data, path, base_dir=path.parent)
