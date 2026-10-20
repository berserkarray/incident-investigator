"""Pure Tool functions over an in-memory snapshot (Req 13.3-13.7, 13.9, 13.10, 13.12, 13.13).

Each function returns every matching record, sorted by ``(timestamp, record_id)``; the Tool_Layer
applies the record limit and failure injection afterwards. Time ranges are half-open ``[start, end)``.
The functions read nothing but their arguments, so equal inputs give equal records (Req 13.10).
"""

import math
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta

from ..models.snapshot import IncidentSnapshot, MetricPoint, PastIncident
from ..models.tools import (
    DependencyRecord,
    GetDependenciesInput,
    GetDeploymentsInput,
    MetricBucket,
    QueryLogsInput,
    QueryMetricsInput,
    SearchIncidentsInput,
    ToolInputBase,
    ToolRecord,
    record_timestamp,
)
from ..models.vocab import ComponentKind, ToolName

ToolFn = Callable[[ToolInputBase, IncidentSnapshot], list[ToolRecord]]

_MIN_TIME = datetime.min.replace(tzinfo=UTC)


def sort_records(records: Sequence[ToolRecord]) -> list[ToolRecord]:
    """Ascending timestamp, ties by record ID; records without a timestamp sort first (Req 13.9)."""
    return sorted(records, key=lambda r: (record_timestamp(r) or _MIN_TIME, r.record_id))


def cut_to_limit(records: Sequence[ToolRecord], limit: int) -> tuple[tuple[ToolRecord, ...], bool]:
    """The first ``limit`` records and whether any were left out (Req 13.9)."""
    return tuple(records[:limit]), len(records) > limit


# query_logs


def query_logs(inp: QueryLogsInput, snapshot: IncidentSnapshot) -> list[ToolRecord]:
    text = inp.text.casefold() if inp.text else None
    levels = set(inp.levels) if inp.levels else None
    attributes = inp.attributes or {}
    matches = [
        record
        for record in snapshot.logs
        if record.service == inp.service
        and inp.time_range.contains(record.timestamp)
        and (levels is None or record.level in levels)
        and (text is None or text in record.message.casefold())
        and all(record.attributes.get(k) == v for k, v in attributes.items())
    ]
    return sort_records(matches)


# query_metrics


def bucket_record_id(service: str, metric_name: str, bucket_start: datetime, step_seconds: int) -> str:
    """``"{series}:{epoch}:{step}"`` where the series is ``service/metric_name``."""
    return f"{service}/{metric_name}:{int(bucket_start.timestamp())}:{step_seconds}"


def query_metrics(inp: QueryMetricsInput, snapshot: IncidentSnapshot) -> list[ToolRecord]:
    """One bucket of ``step_seconds`` per non-empty aggregation step, aligned to the range start (Req 13.4)."""
    step = timedelta(seconds=inp.step_seconds)
    start = inp.time_range.start
    buckets: dict[int, list[MetricPoint]] = {}
    for point in snapshot.metrics:
        if (
            point.service == inp.service
            and point.metric_name == inp.metric_name
            and inp.time_range.contains(point.timestamp)
        ):
            buckets.setdefault((point.timestamp - start) // step, []).append(point)
    records: list[ToolRecord] = []
    for index in sorted(buckets):
        points = sorted(buckets[index], key=lambda p: (p.timestamp, p.record_id))
        values = [p.value for p in points]
        bucket_start = start + index * step
        records.append(
            MetricBucket(
                record_id=bucket_record_id(inp.service, inp.metric_name, bucket_start, inp.step_seconds),
                timestamp=bucket_start,
                service=inp.service,
                metric_name=inp.metric_name,
                step_seconds=inp.step_seconds,
                mean=round(math.fsum(values) / len(values), 6),
                min=min(values),
                max=max(values),
                count=len(values),
                constituent_ids=tuple(p.record_id for p in points),
            )
        )
    return sort_records(records)


# get_deployments


def get_deployments(inp: GetDeploymentsInput, snapshot: IncidentSnapshot) -> list[ToolRecord]:
    """Deployment, config-change and feature-flag-change events in range, optionally filtered (Req 13.5)."""
    change_types = set(inp.change_types) if inp.change_types else None
    matches = [
        change
        for change in snapshot.changes
        if inp.time_range.contains(change.timestamp)
        and (inp.service is None or change.service == inp.service)
        and (change_types is None or change.change_type in change_types)
    ]
    return sort_records(matches)


# get_dependencies


def _shortest_paths(snapshot: IncidentSnapshot, service: str, depth: int, upstream: bool) -> dict[str, tuple[str, ...]]:
    """Breadth-first search up to ``depth`` hops in one direction.

    Upstream follows ``dependent -> dependency`` edges (Components the service depends on);
    downstream follows them backwards. Each reached Component keeps its shortest path; among
    equally short paths the lexicographically smallest tuple of edge IDs wins.
    """
    adjacency: dict[str, list[tuple[str, str]]] = {}
    for edge in snapshot.dependencies:
        source, target = (edge.dependent, edge.dependency) if upstream else (edge.dependency, edge.dependent)
        adjacency.setdefault(source, []).append((edge.record_id, target))
    reached: dict[str, tuple[str, ...]] = {service: ()}
    frontier: dict[str, tuple[str, ...]] = {service: ()}
    for _ in range(depth):
        layer: dict[str, tuple[str, ...]] = {}
        for node, path in frontier.items():
            for edge_id, neighbour in adjacency.get(node, ()):
                if neighbour in reached:
                    continue
                candidate = (*path, edge_id)
                if neighbour not in layer or candidate < layer[neighbour]:
                    layer[neighbour] = candidate
        if not layer:
            break
        reached.update(layer)
        frontier = layer
    del reached[service]
    return reached


def get_dependencies(inp: GetDependenciesInput, snapshot: IncidentSnapshot) -> list[ToolRecord]:
    """Every Component within ``depth`` hops, labelled upstream or downstream with its hop count (Req 13.6).

    ``record_id`` is the final-hop edge ID and ``constituent_ids`` the path's edge IDs.
    """
    kinds = {c.name: c.kind for c in snapshot.components}
    records: list[ToolRecord] = []
    for direction, upstream in (("upstream", True), ("downstream", False)):
        for component, path in _shortest_paths(snapshot, inp.service, inp.depth, upstream).items():
            records.append(
                DependencyRecord(
                    record_id=path[-1],
                    component=component,
                    component_kind=kinds.get(component, ComponentKind.SERVICE),
                    direction=direction,
                    hops=len(path),
                    constituent_ids=path,
                )
            )
    return sort_records(records)


# search_incidents


def query_terms(query: str) -> tuple[str, ...]:
    """Distinct, case-folded, whitespace-separated terms in first-seen order."""
    return tuple(dict.fromkeys(term.casefold() for term in query.split()))


def incident_text(incident: PastIncident) -> str:
    return "\n".join((incident.title, incident.summary, *incident.symptoms, incident.resolution)).casefold()


def matched_term_count(query: str, incident: PastIncident) -> int:
    text = incident_text(incident)
    return sum(1 for term in query_terms(query) if term in text)


def search_incidents(inp: SearchIncidentsInput, snapshot: IncidentSnapshot) -> list[ToolRecord]:
    """Past incidents whose title, summary, symptoms or resolution contain at least one query term (Req 13.7)."""
    terms = query_terms(inp.query)
    matches = []
    for incident in snapshot.past_incidents:
        if inp.service is not None and inp.service not in incident.affected_services:
            continue
        text = incident_text(incident)
        if any(term in text for term in terms):
            matches.append(incident)
    return sort_records(matches)


TOOL_REGISTRY: dict[ToolName, ToolFn] = {
    ToolName.QUERY_LOGS: query_logs,
    ToolName.QUERY_METRICS: query_metrics,
    ToolName.GET_DEPLOYMENTS: get_deployments,
    ToolName.GET_DEPENDENCIES: get_dependencies,
    ToolName.SEARCH_INCIDENTS: search_incidents,
}
"""The five Tools (Req 13.1)."""
