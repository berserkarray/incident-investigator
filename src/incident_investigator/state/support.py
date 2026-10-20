"""Support_Check: does a cited Evidence_Item support the statement that cites it? (design D2, Req 27.4)

Deterministic, rule-based and lexical. The Evaluator uses it for citation precision; it reads only
the cited Evidence_Item and its retained records, never an Answer_Key.

Historical_Evidence (search_incidents results) supports only past-incident claims, because it
describes a different incident. Every other rule therefore requires CURRENT, including the
free-text rule, which the D2 table lists without it.
"""

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ..models.claims import ClaimDraft, DependencyClaim, LogSignal, MetricSignal, ObservabilityClaim
from ..models.evidence import EvidenceItem
from ..models.incident import IncidentClaimKind
from ..models.snapshot import ChangeEvent, LogRecord, PastIncident
from ..models.tools import DependencyRecord, MetricBucket, ToolRecord, input_service
from ..models.vocab import ChangeType, FindingType, ToolName

LOG_ONSET_TOLERANCE = timedelta(seconds=300)
"""A log claim's onset must be within this distance of a cited record of the claimed level."""

TELEMETRY_TOOLS = frozenset({ToolName.QUERY_LOGS, ToolName.QUERY_METRICS})

_CHANGE_FOR_FINDING = {
    FindingType.DEPLOYMENT: ChangeType.DEPLOYMENT,
    FindingType.CONFIG_CHANGE: ChangeType.CONFIG_CHANGE,
    FindingType.FEATURE_FLAG_CHANGE: ChangeType.FEATURE_FLAG_CHANGE,
}


@dataclass(frozen=True)
class HypothesisCitation:
    """A Hypothesis citation. ``contradicting=False`` covers supporting citations and Recommended_Action
    citations, which are judged through the Component of the action's Hypothesis."""

    component: str
    contradicting: bool = False


@dataclass(frozen=True)
class IncidentClaimCitation:
    """A corroborating (or contradicting) citation of one Incident_Report claim."""

    claim_kind: IncidentClaimKind
    affected_service: str
    start_time: datetime
    contradicting: bool = False


CitingStatement = ObservabilityClaim | DependencyClaim | ClaimDraft | HypothesisCitation | IncidentClaimCitation


def _norm(value: str) -> str:
    return value.strip().lower()


def _word_pattern(name: str) -> re.Pattern[str]:
    # Component names contain hyphens, so a hyphen counts as part of a word.
    return re.compile(rf"(?<![\w-]){re.escape(name)}(?![\w-])", re.IGNORECASE)


def _texts(record: ToolRecord) -> list[str]:
    """Free text in which a Component may occur as a whole word."""
    if isinstance(record, LogRecord):
        return [record.message, *record.attributes.values()]
    if isinstance(record, ChangeEvent):
        return [record.summary, *record.attributes.values()]
    if isinstance(record, PastIncident):
        return [record.title, record.summary]
    return []


def _names(record: ToolRecord) -> list[str]:
    """Service or Component fields compared for equality."""
    if isinstance(record, (LogRecord, ChangeEvent, MetricBucket)):
        return [record.service]
    if isinstance(record, DependencyRecord):
        return [record.component]
    if isinstance(record, PastIncident):
        return list(record.affected_services)
    return []


def _to_second(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(microsecond=0)


class SupportCheck:
    # base predicates

    @staticmethod
    def current(e: EvidenceItem) -> bool:
        return e.tool is not ToolName.SEARCH_INCIDENTS

    @staticmethod
    def nonempty(records: Sequence[ToolRecord]) -> bool:
        return len(records) > 0

    @staticmethod
    def concerns(e: EvidenceItem, records: Sequence[ToolRecord], component: str) -> bool:
        wanted = _norm(component)
        if not wanted:
            return False
        service = input_service(e.query)
        if service is not None and _norm(service) == wanted:
            return True
        pattern = _word_pattern(component.strip())
        for record in records:
            if any(_norm(name) == wanted for name in _names(record)):
                return True
            if any(pattern.search(text) for text in _texts(record)):
                return True
        return False

    @staticmethod
    def covers(e: EvidenceItem, moment: datetime) -> bool:
        return e.time_range is None or e.time_range.contains(moment)

    # the rule table

    def supports(self, stmt: CitingStatement, e: EvidenceItem, records: Sequence[ToolRecord]) -> bool:
        if isinstance(stmt, ObservabilityClaim):
            return self._observability(stmt, e, records)
        if isinstance(stmt, DependencyClaim):
            return self._dependency(stmt, e, records)
        if isinstance(stmt, HypothesisCitation):
            if not self.current(e) or not self.concerns(e, records, stmt.component):
                return False
            return stmt.contradicting or self.nonempty(records)
        if isinstance(stmt, IncidentClaimCitation):
            return self._incident_claim(stmt, e, records)
        if isinstance(stmt, ClaimDraft):
            if not self.current(e) or not self.nonempty(records):
                return False
            return stmt.component is None or self.concerns(e, records, stmt.component)
        raise TypeError(f"unsupported citing statement {type(stmt).__name__}")

    def _observability(self, claim: ObservabilityClaim, e: EvidenceItem, records: Sequence[ToolRecord]) -> bool:
        if not self.current(e) or not self.concerns(e, records, claim.service):
            return False
        signal = claim.signal
        if isinstance(signal, MetricSignal):
            if e.tool is not ToolName.QUERY_METRICS or getattr(e.query, "metric_name", None) is not signal.metric_name:
                return False
            return claim.onset is None or (self.nonempty(records) and self.covers(e, claim.onset))
        assert isinstance(signal, LogSignal)
        if e.tool is not ToolName.QUERY_LOGS:
            return False
        if claim.onset is None:
            return True
        return any(
            isinstance(r, LogRecord) and r.level is signal.level and abs(r.timestamp - claim.onset) <= LOG_ONSET_TOLERANCE
            for r in records
        )

    def _dependency(self, claim: DependencyClaim, e: EvidenceItem, records: Sequence[ToolRecord]) -> bool:
        components = {_norm(c) for c in claim.components}
        if claim.finding_type is FindingType.PAST_INCIDENT:
            return e.tool is ToolName.SEARCH_INCIDENTS and self.nonempty(records)
        if claim.finding_type is FindingType.DEPENDENCY_RELATION:
            if e.tool is not ToolName.GET_DEPENDENCIES:
                return False
            known = {_norm(r.component) for r in records if isinstance(r, DependencyRecord)}
            service = input_service(e.query)
            if service is not None:
                known.add(_norm(service))
            return components <= known
        if not self.current(e) or e.tool is not ToolName.GET_DEPLOYMENTS or claim.event_time is None:
            return False
        change_type = _CHANGE_FOR_FINDING[claim.finding_type]
        when = _to_second(claim.event_time)
        return any(
            isinstance(r, ChangeEvent)
            and r.change_type is change_type
            and _norm(r.service) in components
            and _to_second(r.timestamp) == when
            for r in records
        )

    def _incident_claim(self, stmt: IncidentClaimCitation, e: EvidenceItem, records: Sequence[ToolRecord]) -> bool:
        if not self.current(e):
            return False
        if not stmt.contradicting and not self.nonempty(records):
            return False
        if stmt.claim_kind is IncidentClaimKind.AFFECTED_SERVICE:
            return self.concerns(e, records, stmt.affected_service)
        if stmt.claim_kind is IncidentClaimKind.SYMPTOM:
            return e.tool in TELEMETRY_TOOLS and self.concerns(e, records, stmt.affected_service)
        return e.tool in TELEMETRY_TOOLS and self.covers(e, stmt.start_time)
