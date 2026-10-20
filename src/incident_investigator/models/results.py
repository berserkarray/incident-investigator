"""Run scores and the Results_Table (Req 26, 27). Written only by the Evaluator."""

from datetime import date
from enum import StrEnum, auto
from typing import Literal

from pydantic import Field

from .base import StrictModel
from .vocab import SystemName


class MetricKey(StrEnum):
    # accuracy, grounding and cost
    TOP1_ACCURACY = auto()
    TOP3_ACCURACY = auto()
    CITATION_PRECISION = auto()
    KEY_EVIDENCE_RECALL = auto()
    TOOL_CALLS = auto()
    LATENCY_S = auto()
    INPUT_TOKENS = auto()
    OUTPUT_TOKENS = auto()
    TOTAL_TOKENS = auto()
    # diagnostics, reported separately from accuracy
    RANKING_HIT_AT_3 = auto()
    OVER_CAUTIOUS_RATE = auto()
    OVER_CONFIDENT_RATE = auto()


DIAGNOSTIC_METRICS: frozenset[MetricKey] = frozenset(
    {MetricKey.RANKING_HIT_AT_3, MetricKey.OVER_CAUTIOUS_RATE, MetricKey.OVER_CONFIDENT_RATE}
)


class MetricStat(StrictModel):
    mean: float | None
    spread: float | None
    n: int = Field(ge=0)
    status: Literal["ok", "unavailable", "not_applicable"]


class RunScore(StrictModel):
    scenario: str
    system: SystemName
    run_index: int = Field(ge=1)
    seed: int
    failed: bool
    error: str | None = None
    top1: int = Field(ge=0, le=1)
    top3: int = Field(ge=0, le=1)
    hit_at_3: int | None = None
    over_cautious: int | None = None
    over_confident: int | None = None
    citation_precision: float | None = None
    key_evidence_recall: float | None = None
    tool_calls: int = Field(0, ge=0)
    latency_s: float = Field(0.0, ge=0)
    input_tokens: int = Field(0, ge=0)
    output_tokens: int = Field(0, ge=0)
    total_tokens: int = Field(0, ge=0)
    expected_gaps_found: dict[str, bool] = {}
    isolation_violations: tuple[str, ...] = ()


class ResultsRow(StrictModel):
    scenario: str  # scenario name or "ALL"
    system: SystemName
    runs: int = Field(ge=0)
    failed_runs: int = Field(ge=0)
    metrics: dict[MetricKey, MetricStat]


class ResultsTable(StrictModel):
    provider: str
    model: str
    seed: int
    runs_per_scenario: int = Field(ge=1)
    evaluation_date: date
    scenario_count: int = Field(ge=0)
    eval_command: str
    systems: tuple[SystemName, ...]
    rows: tuple[ResultsRow, ...] = ()
    runs: tuple[RunScore, ...] = ()
