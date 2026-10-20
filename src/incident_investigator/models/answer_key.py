"""Answer_Key: read only by the Evaluator and tests (Req 24).

Investigator packages (tools, state, orchestrator, agents, baseline, llm, report) must not import this module.
"""

from typing import Literal

from pydantic import Field, model_validator

from .base import StrictModel
from .vocab import Caller, CauseType, ToolName


class RootCauseLabel(StrictModel):
    component: str = Field(min_length=1)
    cause_type: CauseType


class ExpectedDataGap(StrictModel):
    source: ToolName | Caller
    reason: str


class AnswerKey(StrictModel):
    """``<Answer_Key_Root>/<incident_id>.json``."""

    schema_version: Literal[1] = 1
    incident_id: str
    scenario_name: str
    seed: int
    outcome: Literal["root_cause", "insufficient_evidence"]
    root_cause: RootCauseLabel | None = None
    contributing_factors: tuple[str, ...] = ()
    key_evidence: tuple[str, ...] = ()
    expected_data_gaps: tuple[ExpectedDataGap, ...] = ()
    decoy_cause_types: tuple[CauseType, ...] = ()
    canary: str = Field(min_length=32)

    @model_validator(mode="after")
    def _label_matches_outcome(self) -> "AnswerKey":
        if (self.outcome == "root_cause") != (self.root_cause is not None):
            raise ValueError("root_cause is present exactly when outcome is root_cause")
        if self.root_cause is not None and not self.key_evidence:
            raise ValueError("an answer key with a root cause lists at least one key evidence record ID")
        return self
