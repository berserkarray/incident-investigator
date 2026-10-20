"""Claims made by Agents and the Baseline_Agent (Req 4, 8.3, 9.3, 9.4)."""

from typing import Annotated, Literal

from pydantic import Field, model_validator

from .base import AwareDatetime, NonBlank500, StrictModel
from .vocab import Caller, ClaimLabel, FindingType, LogLevel, MetricName


class MetricSignal(StrictModel):
    kind: Literal["metric"] = "metric"
    metric_name: MetricName


class LogSignal(StrictModel):
    kind: Literal["log"] = "log"
    level: LogLevel
    pattern: str = Field(min_length=1, max_length=200)


class ObservabilityClaim(StrictModel):
    """One service, one signal, an onset time or a no-onset marker, and at least one citation."""

    kind: Literal["observability"] = "observability"
    text: NonBlank500
    service: str = Field(min_length=1, max_length=100)
    signal: Annotated[MetricSignal | LogSignal, Field(discriminator="kind")]
    onset: AwareDatetime | None = None
    no_onset: bool = False
    evidence_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _onset_xor_marker(self) -> "ObservabilityClaim":
        if (self.onset is None) == (not self.no_onset):
            raise ValueError("give exactly one of onset or no_onset=true")
        return self


class DependencyClaim(StrictModel):
    kind: Literal["dependency"] = "dependency"
    text: NonBlank500
    components: tuple[str, ...] = Field(min_length=1)
    finding_type: FindingType
    event_time: AwareDatetime | None = None
    historical: bool = False
    evidence_ids: tuple[str, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _event_time_rules(self) -> "DependencyClaim":
        if self.finding_type is not FindingType.DEPENDENCY_RELATION and self.event_time is None:
            raise ValueError(f"event_time is required for finding_type {self.finding_type}")
        if (self.finding_type is FindingType.PAST_INCIDENT) != self.historical:
            raise ValueError("historical must be true exactly for past_incident findings")
        return self


class ClaimDraft(StrictModel):
    """A free-text claim (Reporting summary, Baseline claim)."""

    kind: Literal["free"] = "free"
    text: NonBlank500
    evidence_ids: tuple[str, ...] = ()
    component: str | None = None


ClaimBody = Annotated[ObservabilityClaim | DependencyClaim | ClaimDraft, Field(discriminator="kind")]


class LabelledClaim(StrictModel):
    """A Claim after the Citation_Validator, as stored on the Blackboard."""

    claim_id: str
    producer: Caller
    task_id: str | None
    body: ClaimBody
    label: ClaimLabel
