"""Support_Check rule table (design D2, Req 27.4): one example per row, plus its edge cases."""

from datetime import timedelta

import pytest
from factories import at, window

from incident_investigator.models.claims import ClaimDraft, DependencyClaim, LogSignal, MetricSignal, ObservabilityClaim
from incident_investigator.models.evidence import EvidenceItem, ObservationSummary
from incident_investigator.models.incident import IncidentClaimKind
from incident_investigator.models.snapshot import ChangeEvent, LogRecord, PastIncident
from incident_investigator.models.tools import (
    DependencyRecord,
    GetDependenciesInput,
    GetDeploymentsInput,
    MetricBucket,
    QueryLogsInput,
    QueryMetricsInput,
    SearchIncidentsInput,
)
from incident_investigator.models.vocab import AgentName, ChangeType, ComponentKind, LogLevel, MetricName, Reliability
from incident_investigator.state.support import HypothesisCitation, IncidentClaimCitation, SupportCheck

CHECK = SupportCheck()
SERVICE = "checkout-a1b"


def item(query, time_range="auto") -> EvidenceItem:
    return EvidenceItem(
        evidence_id="E0001",
        seq=1,
        tool=query.tool,
        query=query,
        observation=ObservationSummary(record_count=0),
        time_range=getattr(query, "time_range", None) if time_range == "auto" else time_range,
        reliability=Reliability.COMPLETE,
        source_record_ids=(),
        produced_by=AgentName.OBSERVABILITY,
        task_id="T1",
        recorded_at=at(0),
    )


LOGS = item(QueryLogsInput(service=SERVICE, time_range=window(0, 120)))
LOG_RECORDS = (
    LogRecord(record_id="l1", timestamp=at(61), service=SERVICE, level=LogLevel.ERROR, message="pool exhausted on orders-db-456"),
)
METRICS = item(QueryMetricsInput(service=SERVICE, metric_name=MetricName.ERROR_RATE, time_range=window(0, 120)))
BUCKETS = (
    MetricBucket(
        record_id="b1", timestamp=at(60), service=SERVICE, metric_name=MetricName.ERROR_RATE, step_seconds=300,
        mean=0.2, min=0.1, max=0.3, count=2, constituent_ids=("m1", "m2"),
    ),
)
DEPLOYS = item(GetDeploymentsInput(time_range=window(0, 120)))
CHANGES = (
    ChangeEvent(
        record_id="c1", timestamp=at(50).replace(microsecond=400), service=SERVICE,
        change_type=ChangeType.CONFIG_CHANGE, summary="pool size 50 -> 5",
    ),
)
DEPS = item(GetDependenciesInput(service=SERVICE, depth=2))
DEP_RECORDS = (
    DependencyRecord(record_id="d1", component="orders-db-456", component_kind=ComponentKind.DATABASE, direction="upstream", hops=1, constituent_ids=("d1",)),
)
HISTORY = item(SearchIncidentsInput(query="pool"))
PAST = (
    PastIncident(
        record_id="p1", occurred_at=at(-10000), title="Pool exhaustion on checkout-a1b", summary="s",
        affected_services=(SERVICE,), symptoms=("errors",), cause_type="connection_pool_exhaustion", resolution="r",
    ),
)


def obs_claim(signal, onset=None) -> ObservabilityClaim:
    return ObservabilityClaim(
        text="t", service=SERVICE, signal=signal, onset=onset, no_onset=onset is None, evidence_ids=("E0001",)
    )


def dep_claim(finding_type, components=(SERVICE,), event_time=None) -> DependencyClaim:
    return DependencyClaim(
        text="t", components=components, finding_type=finding_type, event_time=event_time,
        historical=finding_type == "past_incident", evidence_ids=("E0001",),
    )


def test_observability_metric_claims():
    signal = MetricSignal(metric_name=MetricName.ERROR_RATE)
    assert CHECK.supports(obs_claim(signal, onset=at(60)), METRICS, BUCKETS)
    assert not CHECK.supports(obs_claim(signal, onset=at(130)), METRICS, BUCKETS)  # onset outside the range
    assert not CHECK.supports(obs_claim(signal, onset=at(60)), METRICS, ())  # an onset needs records
    assert CHECK.supports(obs_claim(signal), METRICS, ())  # no-onset claims need no records
    other = MetricSignal(metric_name=MetricName.CPU_UTILIZATION)
    assert not CHECK.supports(obs_claim(other), METRICS, BUCKETS)
    assert not CHECK.supports(obs_claim(signal), LOGS, LOG_RECORDS)


def test_observability_log_claims():
    signal = LogSignal(level=LogLevel.ERROR, pattern="pool")
    assert CHECK.supports(obs_claim(signal, onset=at(61) + timedelta(seconds=300)), LOGS, LOG_RECORDS)
    assert not CHECK.supports(obs_claim(signal, onset=at(61) + timedelta(seconds=301)), LOGS, LOG_RECORDS)
    warn = LogSignal(level=LogLevel.WARN, pattern="pool")
    assert not CHECK.supports(obs_claim(warn, onset=at(61)), LOGS, LOG_RECORDS)
    assert CHECK.supports(obs_claim(warn), LOGS, ())


def test_dependency_change_claims_match_type_component_and_second():
    claim = dep_claim("config_change", event_time=at(50))
    assert CHECK.supports(claim, DEPLOYS, CHANGES)  # microseconds are ignored
    assert not CHECK.supports(dep_claim("deployment", event_time=at(50)), DEPLOYS, CHANGES)
    assert not CHECK.supports(dep_claim("config_change", event_time=at(51)), DEPLOYS, CHANGES)
    assert not CHECK.supports(dep_claim("config_change", components=("payments-c2d",), event_time=at(50)), DEPLOYS, CHANGES)


def test_dependency_relation_and_past_incident_claims():
    assert CHECK.supports(dep_claim("dependency_relation", components=(SERVICE, "orders-db-456")), DEPS, DEP_RECORDS)
    assert not CHECK.supports(dep_claim("dependency_relation", components=("card-api-abc",)), DEPS, DEP_RECORDS)
    assert CHECK.supports(dep_claim("past_incident", event_time=at(-10000)), HISTORY, PAST)
    assert not CHECK.supports(dep_claim("past_incident", event_time=at(-10000)), HISTORY, ())


@pytest.mark.parametrize(
    ("component", "records", "contradicting", "expected"),
    [
        (SERVICE, LOG_RECORDS, False, True),  # query service
        ("orders-db-456", LOG_RECORDS, False, True),  # whole word in a message
        ("orders-db", LOG_RECORDS, False, False),  # a hyphenated prefix is not a whole word
        (SERVICE, (), False, False),  # supporting needs records
        (SERVICE, (), True, True),  # contradicting does not
    ],
)
def test_hypothesis_citations(component, records, contradicting, expected):
    assert CHECK.supports(HypothesisCitation(component, contradicting), LOGS, records) is expected


def test_historical_evidence_supports_only_past_incident_claims():
    assert not CHECK.supports(HypothesisCitation(SERVICE), HISTORY, PAST)
    assert not CHECK.supports(ClaimDraft(text="t", evidence_ids=("E0001",)), HISTORY, PAST)
    citation = IncidentClaimCitation(IncidentClaimKind.AFFECTED_SERVICE, SERVICE, at(60))
    assert not CHECK.supports(citation, HISTORY, PAST)


def test_incident_claim_citations():
    def cite(kind, contradicting=False):
        return IncidentClaimCitation(kind, SERVICE, at(60), contradicting)

    assert CHECK.supports(cite(IncidentClaimKind.SYMPTOM), METRICS, BUCKETS)
    assert not CHECK.supports(cite(IncidentClaimKind.SYMPTOM), DEPLOYS, CHANGES)  # symptoms need telemetry
    assert CHECK.supports(cite(IncidentClaimKind.AFFECTED_SERVICE), DEPLOYS, CHANGES)
    assert CHECK.supports(cite(IncidentClaimKind.START_TIME), LOGS, LOG_RECORDS)
    late = IncidentClaimCitation(IncidentClaimKind.START_TIME, SERVICE, at(200))
    assert not CHECK.supports(late, LOGS, LOG_RECORDS)
    assert not CHECK.supports(cite(IncidentClaimKind.SYMPTOM), LOGS, ())
    assert CHECK.supports(cite(IncidentClaimKind.SYMPTOM, contradicting=True), LOGS, ())


def test_free_text_claims():
    assert CHECK.supports(ClaimDraft(text="t", evidence_ids=("E0001",)), LOGS, LOG_RECORDS)
    assert CHECK.supports(ClaimDraft(text="t", component="orders-db-456"), LOGS, LOG_RECORDS)
    assert not CHECK.supports(ClaimDraft(text="t", component="card-api-abc"), LOGS, LOG_RECORDS)
    assert not CHECK.supports(ClaimDraft(text="t"), LOGS, ())
