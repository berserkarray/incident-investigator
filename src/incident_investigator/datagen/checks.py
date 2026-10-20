"""Verification before any write (Req 22.7-22.9, 22.13, 22.15, 22.16, 24.5, 24.11).

Catalogue condition checks per scenario (Req 23.1-23.12) are added in task 5.9.
"""

import re
from collections import defaultdict
from typing import TYPE_CHECKING

from ..models.vocab import LogLevel

if TYPE_CHECKING:
    from .generator import GeneratedIncident

BANNED_MARKERS = (
    "root cause",
    "root_cause",
    "key_evidence",
    "key evidence",
    "red herring",
    "red_herring",
    "decoy",
    "trap",
    "contributing",
    "answer key",
    "answer_key",
)


def _word_pattern(term: str) -> re.Pattern:
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", re.IGNORECASE)


def generic_checks(g: "GeneratedIncident") -> list[str]:
    problems: list[str] = []
    snap, spec, key = g.snapshot, g.spec, g.answer_key
    window = snap.telemetry_window
    names = snap.component_names

    if len(snap.components) != spec.service_count or not 8 <= len(snap.components) <= 15:
        problems.append(f"service count {len(snap.components)} does not match the spec ({spec.service_count})")
    for record in (*snap.logs, *snap.metrics):
        if not window.contains(record.timestamp):
            problems.append(f"telemetry record {record.record_id} is outside the telemetry window")
            break
    lookback_start = g.lookback_start
    if any(not (lookback_start <= c.timestamp < window.end) for c in snap.changes):
        problems.append("a change event lies outside [lookback start, window end)")
    if not any(c.timestamp < window.start for c in snap.changes):
        problems.append("no change event before the telemetry window start")

    key_ids = set(key.key_evidence)
    noise_errors = [r for r in snap.logs if r.level is LogLevel.ERROR and r.record_id not in key_ids]
    if len(noise_errors) < 5:
        problems.append(f"only {len(noise_errors)} background ERROR logs outside Key_Evidence (need 5)")
    if len([c for c in snap.changes if c.record_id not in key_ids]) < 2:
        problems.append("fewer than 2 background change events outside Key_Evidence")
    if not any(r.attributes.get("monitor") and r.record_id not in key_ids for r in snap.logs):
        problems.append("no noisy alert log record outside Key_Evidence")
    series: dict[tuple[str, str], set[float]] = defaultdict(set)
    for point in snap.metrics:
        series[(point.service, point.metric_name)].add(point.value)
    flat = [k for k, values in series.items() if len(values) < 2]
    if flat:
        problems.append(f"metric series without jitter: {flat[:3]}")

    all_ids = [r.record_id for r in (*snap.logs, *snap.metrics, *snap.changes, *snap.past_incidents, *snap.dependencies)]
    if len(all_ids) != len(set(all_ids)):
        problems.append("record IDs are not unique within the snapshot")
    known = set(all_ids)
    missing = sorted(key_ids - known)
    if missing:
        problems.append(f"Key_Evidence record IDs missing from the snapshot: {missing}")
    if key.root_cause is not None:
        if not key.key_evidence:
            problems.append("an answer key with a root cause lists no Key_Evidence")
        if key.root_cause.component not in names:
            problems.append(f"root cause component {key.root_cause.component} is not in the dependency map")
    if g.report.affected_service not in names:
        problems.append("the affected service is not in the dependency map")
    if not window.contains(g.report.start_time):
        problems.append("the incident report start time is outside the telemetry window")
    lowered = snap.incident_id.lower()
    if spec.name in lowered or spec.fault.template.value in lowered or any(
        c.value in lowered for c in ([spec.fault.cause_type] if spec.fault.cause_type else [])
    ):
        problems.append("the incident ID is not opaque")
    return problems


def leak_scan(texts: dict[str, str], g: "GeneratedIncident") -> list[str]:
    """No canary, scenario or template name, or root-cause marker in any Snapshot_Root file (Req 24.5, 24.11)."""
    problems = []
    terms = [g.answer_key.canary, g.spec.name, g.spec.fault.template.value, *BANNED_MARKERS]
    for filename, text in texts.items():
        for term in terms:
            if _word_pattern(term).search(text):
                problems.append(f"{filename} contains the forbidden marker {term!r}")
    return problems
