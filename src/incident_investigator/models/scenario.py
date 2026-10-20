"""Scenario_Specs, Traps and Fault_Template outputs (Req 22, 23).

Read only by ``datagen``. Investigator packages must not import this module.
"""

from enum import StrEnum, auto

from pydantic import Field, model_validator

from .answer_key import ExpectedDataGap, RootCauseLabel
from .base import JsonValue, NonBlank200, NonBlank500, StrictModel
from .snapshot import ChangeEvent, FailureProfile, LogRecord, MetricPoint, PastIncident
from .vocab import CauseType, Severity


class ComponentRole(StrEnum):
    ENTRY_LB = auto()
    AFFECTED = auto()
    UPSTREAM = auto()
    PEER = auto()
    PRIMARY_DB = auto()
    CACHE = auto()
    QUEUE = auto()
    THIRD_PARTY = auto()


class FaultTemplateName(StrEnum):
    CONFIG_REGRESSION = auto()
    DATABASE_FAILOVER = auto()
    THIRD_PARTY_OUTAGE = auto()
    MEMORY_LEAK = auto()
    CERTIFICATE_EXPIRY = auto()
    CACHE_STAMPEDE = auto()
    LB_MISCONFIGURATION = auto()
    DISK_FULL = auto()
    CONNECTION_POOL_EXHAUSTION = auto()
    SYMPTOMS_ONLY = auto()
    FEATURE_FLAG_REGRESSION = auto()
    CASCADING_FAILURE = auto()


class TrapKind(StrEnum):
    UNRELATED_DEPLOY_NEAR_ONSET = auto()
    NOISY_ALERTS = auto()
    DISAGREEING_MONITORS = auto()
    LOOKALIKE_PAST_INCIDENT = auto()
    TRAFFIC_SPIKE = auto()
    INNOCENT_CONFIG_CHANGE = auto()


class CatalogueCondition(StrEnum):
    """One condition per Req 23.1 .. 23.12."""

    BAD_CONFIG_BASELINE = auto()
    DB_FAILOVER_DECOY_DEPLOY = auto()
    THIRD_PARTY_NO_CHANGES = auto()
    SLOW_LEAK_OLD_CHANGE = auto()
    TLS_NO_PATH_DEPLOYS = auto()
    CACHE_STAMPEDE_PEAK = auto()
    LB_DISAGREEING_MONITORS = auto()
    DISK_FULL_LOOKALIKE = auto()
    LOGS_TIMEOUT_GAP = auto()
    INSUFFICIENT_EVIDENCE = auto()
    FLAG_COHORT_ERRORS = auto()
    CASCADING_ERROR_LEADER = auto()


class RedHerring(StrictModel):
    kind: TrapKind
    target_role: ComponentRole | None = None
    offset_minutes: int = 0  # relative to Symptom_Onset
    params: dict[str, JsonValue] = {}


class Trap(StrictModel):
    red_herrings: tuple[RedHerring, ...] = Field(min_length=1)
    decoy_cause_type: CauseType


class FaultSpec(StrictModel):
    template: FaultTemplateName
    root_role: ComponentRole | None = None
    cause_type: CauseType | None = None
    params: dict[str, JsonValue] = {}

    @model_validator(mode="after")
    def _root_and_cause_together(self) -> "FaultSpec":
        if (self.root_role is None) != (self.cause_type is None):
            raise ValueError("root_role and cause_type are both set or both omitted")
        return self


class IncidentReportSpec(StrictModel):
    title: NonBlank200
    symptoms: tuple[NonBlank500, ...] = Field(min_length=1, max_length=10)
    severity: Severity
    start_delay_minutes: int = Field(2, ge=0)


class NoiseSpec(StrictModel):
    error_logs: int = Field(8, ge=5)
    change_events: int = Field(4, ge=2)
    noisy_alerts: int = Field(2, ge=1)


class ScenarioSpec(StrictModel):
    """``scenarios/<name>.json``."""

    name: str = Field(pattern=r"^[a-z0-9]+(-[a-z0-9]+)*$")
    description: str
    service_count: int = Field(ge=8, le=15)
    telemetry_window_hours: float = Field(ge=2, le=24)
    change_lookback_days: int = Field(7, ge=7, le=30)
    symptom_onset_offset_minutes: int = Field(ge=31)
    fault: FaultSpec
    trap: Trap | None = None
    failure_profile: FailureProfile = FailureProfile()
    report: IncidentReportSpec
    noise: NoiseSpec = NoiseSpec()
    conditions: tuple[CatalogueCondition, ...] = ()

    @model_validator(mode="after")
    def _onset_inside_window(self) -> "ScenarioSpec":
        window_minutes = self.telemetry_window_hours * 60
        if self.symptom_onset_offset_minutes + self.report.start_delay_minutes >= window_minutes:
            raise ValueError("symptom onset plus report start delay must fall inside the telemetry window")
        return self


class NoiseConstraints(StrictModel):
    no_changes_in_window: bool = False
    no_deploys_on_path_to_root: bool = False
    no_error_logs_on_root_after_onset: bool = False


class FaultOutput(StrictModel):
    logs: tuple[LogRecord, ...] = ()
    metrics: tuple[MetricPoint, ...] = ()
    changes: tuple[ChangeEvent, ...] = ()
    past_incidents: tuple[PastIncident, ...] = ()
    key_evidence: tuple[str, ...] = ()
    root_cause: RootCauseLabel | None = None
    contributing_factors: tuple[str, ...] = ()
    expected_gaps: tuple[ExpectedDataGap, ...] = ()
    noise_constraints: NoiseConstraints = NoiseConstraints()
