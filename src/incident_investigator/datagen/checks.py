"""Verification before any write (Req 22.7-22.10, 22.13, 22.15, 22.16, 23.1-23.12, 23.15, 24.5, 24.11).

Every check reads the generated output, not the template's intent, so a template bug surfaces here
as a ``GenerationError`` instead of a silently wrong scenario.
"""

from collections import Counter, defaultdict
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from ..models.scenario import CatalogueCondition
from ..models.snapshot import ChangeEvent, DependencyEdge, IncidentSnapshot, LogRecord, MetricPoint, PastIncident
from ..models.vocab import CauseType, ChangeType, ComponentKind, ErrorCode, FailureType, LogLevel, MetricName, ToolName

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


def _is_word_char(char: str) -> bool:
    return char.isascii() and (char.isalnum() or char == "_")


def contains_word(text: str, term: str) -> bool:
    """Case-insensitive whole-word search: ``term`` not preceded or followed by ``[A-Za-z0-9_]``."""
    return _contains_lowered(text.lower(), term.lower())


def _contains_lowered(haystack: str, needle: str) -> bool:
    start = haystack.find(needle)
    while start != -1:
        end = start + len(needle)
        before = haystack[start - 1] if start > 0 else ""
        after = haystack[end] if end < len(haystack) else ""
        if not (before and _is_word_char(before)) and not (after and _is_word_char(after)):
            return True
        start = haystack.find(needle, start + 1)
    return False


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
        past_ids = {p.record_id for p in snap.past_incidents}
        if not key_ids - past_ids:  # Req 23.15
            problems.append("every Key_Evidence record is a past incident")
    if g.report.affected_service not in names:
        problems.append("the affected service is not in the dependency map")
    if not window.contains(g.report.start_time):
        problems.append("the incident report start time is outside the telemetry window")
    lowered = snap.incident_id.lower()
    if spec.name in lowered or spec.fault.template.value in lowered or any(
        c.value in lowered for c in ([spec.fault.cause_type] if spec.fault.cause_type else [])
    ):
        problems.append("the incident ID is not opaque")

    # Red herrings (Req 22.10): each declared one is present, and none is Key_Evidence.
    declared = spec.trap.red_herrings if spec.trap else ()
    for index, herring in enumerate(declared):
        produced = g.herring_record_ids[index] if index < len(g.herring_record_ids) else ()
        if not produced:
            problems.append(f"trap.red_herrings[{index}] ({herring.kind}) produced no records")
        elif not set(produced) <= known:
            problems.append(f"trap.red_herrings[{index}] ({herring.kind}) records are missing from the snapshot")
        overlap = sorted(set(produced) & key_ids)
        if overlap:
            problems.append(f"trap.red_herrings[{index}] ({herring.kind}) records are in Key_Evidence: {overlap[:3]}")
    return problems


def leak_scan(texts: dict[str, str], g: "GeneratedIncident") -> list[str]:
    """No canary, scenario or template name, or root-cause marker in any Snapshot_Root file (Req 24.5, 24.11)."""
    problems = []
    terms = [g.answer_key.canary, g.spec.name, g.spec.fault.template.value, *BANNED_MARKERS]
    for filename, text in texts.items():
        lowered = text.lower()
        for term in terms:
            if _contains_lowered(lowered, term.lower()):
                problems.append(f"{filename} contains the forbidden marker {term!r}")
    return problems


# catalogue conditions (Req 23.1-23.12)


ConditionCheck = Callable[["GeneratedIncident"], list[str]]


def _root(g: "GeneratedIncident") -> str | None:
    return g.answer_key.root_cause.component if g.answer_key.root_cause else None


def _key_changes(g: "GeneratedIncident") -> list[ChangeEvent]:
    key = set(g.answer_key.key_evidence)
    return [c for c in g.snapshot.changes if c.record_id in key]


def _upstream_closure(edges: tuple[DependencyEdge, ...], start: str) -> set[str]:
    """Every Component that ``start`` depends on, directly or transitively."""
    found: set[str] = set()
    todo = [start]
    while todo:
        node = todo.pop()
        for edge in edges:
            if edge.dependent == node and edge.dependency not in found:
                found.add(edge.dependency)
                todo.append(edge.dependency)
    return found


def _on_paths(edges: tuple[DependencyEdge, ...], start: str, goal: str) -> set[str]:
    """Components on any dependency path from ``start`` to ``goal`` (both included when a path exists)."""
    if start == goal:
        return {start}
    reachable = _upstream_closure(edges, start)
    if goal not in reachable:
        return {goal}
    return {start, goal} | {n for n in reachable if goal in _upstream_closure(edges, n)}


def _series(snapshot: IncidentSnapshot, service: str, metric: MetricName) -> list[MetricPoint]:
    return sorted(
        (p for p in snapshot.metrics if p.service == service and p.metric_name == metric),
        key=lambda p: (p.timestamp, p.record_id),
    )


def _needs_root(g: "GeneratedIncident") -> list[str]:
    return [] if _root(g) is not None else ["the answer key has no root cause"]


def check_bad_config_baseline(g: "GeneratedIncident") -> list[str]:
    root, window = _root(g), g.snapshot.telemetry_window
    problems = _needs_root(g)
    if g.spec.trap is not None:
        problems.append("the baseline scenario declares a trap")
    if root and not any(
        c.change_type is ChangeType.CONFIG_CHANGE and c.service == root and window.contains(c.timestamp) and c.timestamp < g.onset
        for c in _key_changes(g)
    ):
        problems.append("no Key_Evidence config change on the root Component inside the window and before onset")
    return problems


def check_db_failover_decoy_deploy(g: "GeneratedIncident") -> list[str]:
    root = _root(g)
    problems = _needs_root(g)
    if root and g.snapshot.component_kind(root) is not ComponentKind.DATABASE:
        problems.append(f"root Component {root} is not a database")
    key = set(g.answer_key.key_evidence)
    decoys = [
        c
        for c in g.snapshot.changes
        if c.change_type is ChangeType.DEPLOYMENT and c.service != root and c.timestamp == g.onset - timedelta(minutes=5)
    ]
    if not decoys:
        problems.append("no deployment on a non-root Component exactly 5 minutes before onset")
    elif all(c.record_id in key for c in decoys):
        problems.append("the deployment 5 minutes before onset is in Key_Evidence")
    if g.spec.trap is None or g.spec.trap.decoy_cause_type is not CauseType.DEPLOY_REGRESSION:
        problems.append("the trap does not name deploy_regression as the decoy")
    return problems


def check_third_party_no_changes(g: "GeneratedIncident") -> list[str]:
    root = _root(g)
    problems = _needs_root(g)
    if root and g.snapshot.component_kind(root) is not ComponentKind.THIRD_PARTY_API:
        problems.append(f"root Component {root} is not a third-party API")
    inside = [c.record_id for c in g.snapshot.changes if g.snapshot.telemetry_window.contains(c.timestamp)]
    if inside:
        problems.append(f"{len(inside)} change events lie inside the telemetry window")
    if g.snapshot.failure_profile.forced_for(ToolName.GET_DEPLOYMENTS) is not None:
        problems.append("the failure profile forces a get_deployments failure")
    return problems


def check_slow_leak_old_change(g: "GeneratedIncident") -> list[str]:
    root = _root(g)
    problems = _needs_root(g)
    if root and not any(
        c.service == root and c.timestamp <= g.onset - timedelta(hours=48) and c.timestamp >= g.lookback_start
        for c in _key_changes(g)
    ):
        problems.append("no Key_Evidence change on the root Component at least 48 h before onset inside the lookback")
    return problems


def check_tls_no_path_deploys(g: "GeneratedIncident") -> list[str]:
    root = _root(g)
    problems = _needs_root(g)
    if root is None:
        return problems
    path = _on_paths(g.snapshot.dependencies, g.report.affected_service, root)
    deploys = [
        c.record_id
        for c in g.snapshot.changes
        if c.change_type is ChangeType.DEPLOYMENT
        and c.service in path
        and g.lookback_start <= c.timestamp < g.snapshot.telemetry_window.end
    ]
    if deploys:
        problems.append(f"{len(deploys)} deployments on the root Component or its dependency path")
    if g.snapshot.failure_profile.forced_for(ToolName.GET_DEPLOYMENTS) is not None:
        problems.append("the failure profile forces a get_deployments failure")
    return problems


def check_cache_stampede_peak(g: "GeneratedIncident") -> list[str]:
    root = _root(g)
    problems = _needs_root(g)
    rate = _series(g.snapshot, g.report.affected_service, MetricName.REQUEST_RATE)
    pre = [p.value for p in rate if g.onset - timedelta(minutes=30) <= p.timestamp < g.onset]
    if not rate or not pre:
        return [*problems, "the affected service has no request-rate series around onset"]
    peak = max(rate, key=lambda p: p.value)
    baseline = sum(pre) / len(pre)
    if not g.onset <= peak.timestamp <= g.onset + timedelta(minutes=5):
        problems.append(f"the request-rate peak at {peak.timestamp.isoformat()} is not within 5 minutes after onset")
    if peak.value < 5 * baseline:
        problems.append(f"the request-rate peak {peak.value} is below 5x the pre-onset mean {baseline:.4g}")
    key = set(g.answer_key.key_evidence)
    if root and not any(
        p.record_id in key and p.timestamp <= peak.timestamp for p in _series(g.snapshot, root, MetricName.CACHE_HIT_RATIO)
    ):
        problems.append("no Key_Evidence cache hit-ratio point of the root Component at or before the peak")
    if g.spec.trap is None or g.spec.trap.decoy_cause_type is not CauseType.TRAFFIC_ATTACK:
        problems.append("the trap does not name traffic_attack as the decoy")
    return problems


def _disagreement_minutes(records: list[LogRecord], healthy: str, unhealthy: str, onset: datetime) -> float:
    """Longest span (minutes) from onset on in which every ``healthy`` record says healthy and every
    ``unhealthy`` record says unhealthy, with both monitors reporting."""
    best = 0.0
    start: datetime | None = None
    last: datetime | None = None
    seen: set[str] = set()
    for record in sorted((r for r in records if r.timestamp >= onset), key=lambda r: (r.timestamp, r.record_id)):
        monitor, status = record.attributes["monitor"], record.attributes.get("status")
        if monitor not in (healthy, unhealthy):
            continue
        consistent = (monitor == healthy and status == "healthy") or (monitor == unhealthy and status == "unhealthy")
        if not consistent:
            start, last, seen = None, None, set()
            continue
        if start is None:
            start = record.timestamp
        last = record.timestamp
        seen.add(monitor)
        if len(seen) == 2:
            best = max(best, (last - start).total_seconds() / 60)
    return best


def check_lb_disagreeing_monitors(g: "GeneratedIncident") -> list[str]:
    root = _root(g)
    problems = _needs_root(g)
    if root and g.snapshot.component_kind(root) is not ComponentKind.LOAD_BALANCER:
        problems.append(f"root Component {root} is not a load balancer")
    by_service: dict[str, list[LogRecord]] = defaultdict(list)
    for record in g.snapshot.logs:
        if "monitor" in record.attributes and record.attributes.get("status") in ("healthy", "unhealthy"):
            by_service[record.service].append(record)
    for records in by_service.values():
        monitors = sorted({r.attributes["monitor"] for r in records})
        for a in monitors:
            for b in monitors:
                if a != b and _disagreement_minutes(records, a, b, g.onset) >= 10:
                    return problems
    return [*problems, "no two monitors disagree about one service for 10 minutes from onset"]


def check_disk_full_lookalike(g: "GeneratedIncident") -> list[str]:
    problems = _needs_root(g)
    key, cause = set(g.answer_key.key_evidence), g.answer_key.root_cause
    decoy = g.spec.trap.decoy_cause_type if g.spec.trap else None
    matches: list[PastIncident] = [
        p
        for p in g.snapshot.past_incidents
        if g.report.affected_service in p.affected_services
        and set(g.report.symptoms) <= set(p.symptoms)
        and (cause is None or p.cause_type is not cause.cause_type)
        and p.record_id not in key
    ]
    if not matches:
        problems.append("no past incident names the affected service, lists every symptom and has a different cause")
    elif not any(p.cause_type is decoy for p in matches):
        problems.append("the trap's decoy cause is not the lookalike past incident's cause")
    return problems


def check_logs_timeout_gap(g: "GeneratedIncident") -> list[str]:
    problems = _needs_root(g)
    forced = g.snapshot.failure_profile.forced
    if [(f.tool, f.failure_type) for f in forced] != [(ToolName.QUERY_LOGS, FailureType.TIMEOUT)]:
        problems.append("the failure profile must force exactly one failure: query_logs timeout")
    if not any(
        str(gap.source) == ToolName.QUERY_LOGS and gap.reason == ErrorCode.TIMEOUT for gap in g.answer_key.expected_data_gaps
    ):
        problems.append("the answer key lists no expected (query_logs, timeout) data gap")
    allowed = {r.record_id for r in (*g.snapshot.metrics, *g.snapshot.changes, *g.snapshot.dependencies)}
    other = sorted(set(g.answer_key.key_evidence) - allowed)
    if other:
        problems.append(f"Key_Evidence holds records other than metrics, changes and dependency edges: {other[:3]}")
    return problems


def check_insufficient_evidence(g: "GeneratedIncident") -> list[str]:
    problems = []
    key = g.answer_key
    if key.outcome != "insufficient_evidence" or key.root_cause is not None or key.key_evidence:
        problems.append("the answer key does not carry the insufficient-evidence marker alone")
    fault = g.fault_record_ids
    if fault.get("changes") or fault.get("past_incidents"):
        problems.append("the fault produced change or past-incident records")
    logs = {r.record_id: r for r in g.snapshot.logs}
    fault_logs = [logs[i] for i in fault.get("logs", ()) if i in logs]
    if not fault_logs:
        problems.append("the fault produced no symptom records for the affected service")
    if any(r.service != g.report.affected_service for r in fault_logs):
        problems.append("the fault produced records outside the affected service")
    return problems


def check_flag_cohort_errors(g: "GeneratedIncident") -> list[str]:
    root = _root(g)
    problems = _needs_root(g)
    if root is None:
        return problems
    flags = [
        c
        for c in _key_changes(g)
        if c.change_type is ChangeType.FEATURE_FLAG_CHANGE and c.service == root and c.timestamp < g.onset and "cohort" in c.attributes
    ]
    if not flags:
        return [*problems, "no Key_Evidence feature-flag change with a cohort on the root Component before onset"]
    cohort = flags[0].attributes["cohort"]
    after = [r for r in g.snapshot.logs if r.service == root and r.timestamp >= g.onset]
    errors = [r for r in after if r.level is LogLevel.ERROR]
    if not errors:
        problems.append("the root Component has no ERROR logs from onset")
    stray = [r.record_id for r in errors if r.attributes.get("cohort") != cohort]
    if stray:
        problems.append(f"{len(stray)} root ERROR logs from onset lack cohort {cohort}")
    if not any(r.attributes.get("cohort") != cohort for r in after):
        problems.append(f"the root Component has no logs without cohort {cohort} from onset")
    return problems


def check_cascading_error_leader(g: "GeneratedIncident") -> list[str]:
    root = _root(g)
    problems = _needs_root(g)
    if root is None:
        return problems
    counts = Counter(
        r.service
        for r in g.snapshot.logs
        if r.level is LogLevel.ERROR and g.onset <= r.timestamp < g.snapshot.telemetry_window.end
    )
    ranked = counts.most_common()
    if not ranked or (len(ranked) > 1 and ranked[0][1] == ranked[1][1]):
        return [*problems, "no Component has the strictly highest ERROR log count from onset"]
    leader = ranked[0][0]
    if leader == root:
        problems.append("the root Component has the most ERROR logs from onset")
    elif root not in _upstream_closure(g.snapshot.dependencies, leader):
        problems.append(f"the ERROR log leader {leader} does not depend on the root Component")
    return problems


CONDITION_CHECKS: dict[CatalogueCondition, ConditionCheck] = {
    CatalogueCondition.BAD_CONFIG_BASELINE: check_bad_config_baseline,
    CatalogueCondition.DB_FAILOVER_DECOY_DEPLOY: check_db_failover_decoy_deploy,
    CatalogueCondition.THIRD_PARTY_NO_CHANGES: check_third_party_no_changes,
    CatalogueCondition.SLOW_LEAK_OLD_CHANGE: check_slow_leak_old_change,
    CatalogueCondition.TLS_NO_PATH_DEPLOYS: check_tls_no_path_deploys,
    CatalogueCondition.CACHE_STAMPEDE_PEAK: check_cache_stampede_peak,
    CatalogueCondition.LB_DISAGREEING_MONITORS: check_lb_disagreeing_monitors,
    CatalogueCondition.DISK_FULL_LOOKALIKE: check_disk_full_lookalike,
    CatalogueCondition.LOGS_TIMEOUT_GAP: check_logs_timeout_gap,
    CatalogueCondition.INSUFFICIENT_EVIDENCE: check_insufficient_evidence,
    CatalogueCondition.FLAG_COHORT_ERRORS: check_flag_cohort_errors,
    CatalogueCondition.CASCADING_ERROR_LEADER: check_cascading_error_leader,
}


def condition_checks(g: "GeneratedIncident") -> list[str]:
    """Every catalogue condition the spec names, each violation prefixed with the condition (Req 23.16)."""
    return [f"{condition}: {problem}" for condition in g.spec.conditions for problem in CONDITION_CHECKS[condition](g)]
