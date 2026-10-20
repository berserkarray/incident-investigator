"""Support_Check: does a cited Evidence_Item support the statement that cites it? (design D2, Req 27.4)

Deterministic, lexical rules over the Evidence_Item, its retained records and the citing statement's
structure. The Evaluator uses it for citation precision. Historical_Evidence (search_incidents)
supports only past-incident claims, because it describes a different incident.

Note: task 6.3 places the shared Support_Check in ``state/support.py`` on the orchestrator-state
workstream. Until that lands, the Evaluator carries this implementation of the same D2 rule table;
``score_run`` takes the checker as a parameter so the two can be swapped without other changes.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

from ..models.claims import ClaimBody, ClaimDraft, DependencyClaim, LogSignal, MetricSignal, ObservabilityClaim
from ..models.evidence import EvidenceItem
from ..models.incident import IncidentClaimKind, IncidentReport
from ..models.snapshot import ChangeEvent, LogRecord, PastIncident
from ..models.tools import DependencyRecord, ToolRecord, input_service
from ..models.vocab import ChangeType, FindingType, ToolName

ONSET_TOLERANCE = timedelta(seconds=300)
"""A log claim's onset must lie within this distance of a record at the claimed level."""

_CHANGE_FOR_FINDING: dict[FindingType, ChangeType] = {
    FindingType.DEPLOYMENT: ChangeType.DEPLOYMENT,
    FindingType.CONFIG_CHANGE: ChangeType.CONFIG_CHANGE,
    FindingType.FEATURE_FLAG_CHANGE: ChangeType.FEATURE_FLAG_CHANGE,
}
_TELEMETRY_TOOLS = frozenset({ToolName.QUERY_LOGS, ToolName.QUERY_METRICS})


# Citing statements


@dataclass(frozen=True)
class ClaimCitation:
    """A Fact: an Observability, Dependency or free-text claim body."""

    body: ClaimBody


@dataclass(frozen=True)
class HypothesisCitation:
    component: str
    contradicting: bool = False


@dataclass(frozen=True)
class ActionCitation:
    """A Recommended_Action citation, judged through its Hypothesis Component (None if unknown)."""

    component: str | None


@dataclass(frozen=True)
class IncidentClaimCitation:
    kind: IncidentClaimKind
    contradicting: bool = False


CitingStatement = ClaimCitation | HypothesisCitation | ActionCitation | IncidentClaimCitation


# Predicates


def _norm(text: str) -> str:
    return text.strip().lower()


def _names(record: ToolRecord) -> Iterable[str]:
    for attr in ("service", "component"):
        value = getattr(record, attr, None)
        if value:
            yield value
    if isinstance(record, PastIncident):
        yield from record.affected_services


def _texts(record: ToolRecord) -> Iterable[str]:
    for attr in ("message", "summary", "title"):
        value = getattr(record, attr, None)
        if value:
            yield value
    yield from getattr(record, "attributes", {}).values()


def current(e: EvidenceItem) -> bool:
    return e.tool is not ToolName.SEARCH_INCIDENTS


def nonempty(records: Sequence[ToolRecord]) -> bool:
    return len(records) > 0


def concerns(e: EvidenceItem, records: Sequence[ToolRecord], component: str) -> bool:
    """The component is the queried service, a record's service or component, or a whole word in record text."""
    target = _norm(component)
    if not target:
        return False
    service = input_service(e.query)
    if service is not None and _norm(service) == target:
        return True
    word = re.compile(rf"(?<![\w-]){re.escape(target)}(?![\w-])", re.IGNORECASE)
    for record in records:
        if any(_norm(name) == target for name in _names(record)):
            return True
        if any(word.search(text) for text in _texts(record)):
            return True
    return False


def covers(e: EvidenceItem, moment: datetime) -> bool:
    return e.time_range is None or e.time_range.contains(moment)


def _same_second(a: datetime, b: datetime) -> bool:
    return a.replace(microsecond=0) == b.replace(microsecond=0)


class SupportCheck:
    """Rule table of design D2. ``incident`` supplies the affected service and start time."""

    def __init__(self, incident: IncidentReport):
        self._affected = incident.affected_service
        self._start = incident.start_time

    def supports(self, stmt: CitingStatement, e: EvidenceItem, records: Sequence[ToolRecord]) -> bool:
        if isinstance(stmt, ClaimCitation):
            return self._claim(stmt.body, e, records)
        if isinstance(stmt, HypothesisCitation):
            if stmt.contradicting:
                return current(e) and concerns(e, records, stmt.component)
            return current(e) and nonempty(records) and concerns(e, records, stmt.component)
        if isinstance(stmt, ActionCitation):
            return (
                stmt.component is not None
                and current(e)
                and nonempty(records)
                and concerns(e, records, stmt.component)
            )
        if isinstance(stmt, IncidentClaimCitation):
            return self._incident_claim(stmt, e, records)
        raise TypeError(f"unknown citing statement {stmt!r}")

    # rules

    def _claim(self, body: ClaimBody, e: EvidenceItem, records: Sequence[ToolRecord]) -> bool:
        if isinstance(body, ObservabilityClaim):
            return self._observability(body, e, records)
        if isinstance(body, DependencyClaim):
            return self._dependency(body, e, records)
        assert isinstance(body, ClaimDraft)
        return nonempty(records) and (body.component is None or concerns(e, records, body.component))

    @staticmethod
    def _observability(claim: ObservabilityClaim, e: EvidenceItem, records: Sequence[ToolRecord]) -> bool:
        if not current(e) or not concerns(e, records, claim.service):
            return False
        signal = claim.signal
        if isinstance(signal, MetricSignal):
            if e.tool is not ToolName.QUERY_METRICS or getattr(e.query, "metric_name", None) != signal.metric_name:
                return False
            return claim.onset is None or (nonempty(records) and covers(e, claim.onset))
        assert isinstance(signal, LogSignal)
        if e.tool is not ToolName.QUERY_LOGS:
            return False
        if claim.onset is None:
            return True
        return any(
            isinstance(r, LogRecord) and r.level == signal.level and abs(r.timestamp - claim.onset) <= ONSET_TOLERANCE
            for r in records
        )

    @staticmethod
    def _dependency(claim: DependencyClaim, e: EvidenceItem, records: Sequence[ToolRecord]) -> bool:
        if claim.finding_type is FindingType.PAST_INCIDENT:
            return e.tool is ToolName.SEARCH_INCIDENTS and nonempty(records)
        if claim.finding_type is FindingType.DEPENDENCY_RELATION:
            if e.tool is not ToolName.GET_DEPENDENCIES:
                return False
            known = {_norm(r.component) for r in records if isinstance(r, DependencyRecord)}
            service = input_service(e.query)
            if service is not None:
                known.add(_norm(service))
            return all(_norm(c) in known for c in claim.components)
        if not current(e) or e.tool is not ToolName.GET_DEPLOYMENTS or claim.event_time is None:
            return False
        change_type = _CHANGE_FOR_FINDING[claim.finding_type]
        components = {_norm(c) for c in claim.components}
        return any(
            isinstance(r, ChangeEvent)
            and r.change_type is change_type
            and _norm(r.service) in components
            and _same_second(r.timestamp, claim.event_time)
            for r in records
        )

    def _incident_claim(self, stmt: IncidentClaimCitation, e: EvidenceItem, records: Sequence[ToolRecord]) -> bool:
        if not current(e):
            return False
        if not stmt.contradicting and not nonempty(records):
            return False
        if stmt.kind is IncidentClaimKind.AFFECTED_SERVICE:
            return concerns(e, records, self._affected)
        if stmt.kind is IncidentClaimKind.SYMPTOM:
            return e.tool in _TELEMETRY_TOOLS and concerns(e, records, self._affected)
        return e.tool in _TELEMETRY_TOOLS and covers(e, self._start)
