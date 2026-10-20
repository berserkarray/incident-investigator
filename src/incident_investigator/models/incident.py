"""Incident_Report input and the claims it contains (Req 1)."""

from enum import StrEnum, auto

from pydantic import Field

from .base import IsoAwareDatetime, NonBlank100, NonBlank200, NonBlank500, StrictModel
from .vocab import ClaimLabel, ClaimStatus, Severity


class IncidentReport(StrictModel):
    """The investigation input. Every symptom, the affected service and the start time are claims to verify."""

    title: NonBlank200
    symptoms: tuple[NonBlank500, ...] = Field(min_length=1, max_length=10)
    affected_service: NonBlank100
    start_time: IsoAwareDatetime
    severity: Severity


class IncidentClaimKind(StrEnum):
    SYMPTOM = auto()
    AFFECTED_SERVICE = auto()
    START_TIME = auto()


class IncidentClaim(StrictModel):
    """One Incident_Report claim and its verification status (Req 1.4, 1.8)."""

    claim_id: str
    kind: IncidentClaimKind
    text: str
    status: ClaimStatus = ClaimStatus.UNVERIFIED
    corroborating_ids: tuple[str, ...] = ()
    contradicting_ids: tuple[str, ...] = ()
    label: ClaimLabel = ClaimLabel.ASSUMPTION


class ClaimStatusUpdate(StrictModel):
    """A Reasoning_Agent or Baseline_Agent status for one claim (Req 1.5)."""

    claim_id: str
    status: ClaimStatus
    corroborating_ids: tuple[str, ...] = ()
    contradicting_ids: tuple[str, ...] = ()
