"""Observation_Summary: compact, deterministic summaries of Tool_Results (Req 3.2, 3.11, design D3).

A summary depends only on the result, the query and the configured size K. It never contains full
records; Agents that need them look records up on the Blackboard by Evidence_ID.
"""

import math
import re
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from ..models.base import TimeRange
from ..models.evidence import MetricStats, ObservationSummary, TopValue
from ..models.snapshot import ChangeEvent, LogRecord, PastIncident
from ..models.tools import (
    DependencyRecord,
    MetricBucket,
    SearchIncidentsInput,
    ToolInputBase,
    ToolRecord,
    ToolResult,
    record_timestamp,
)
from .impl import matched_term_count

KEY_ATTRIBUTES = ("service", "level", "metric_name", "change_type", "cohort", "monitor", "status", "component", "direction")
_DIGITS = re.compile(r"\d+")


def _normalise_message(message: str) -> str:
    return _DIGITS.sub("#", message)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _time_span(records: Sequence[ToolRecord]) -> TimeRange | None:
    stamps = [t for t in (record_timestamp(r) for r in records) if t is not None]
    if not stamps:
        return None
    first, last = min(stamps), max(stamps)
    if first == last:
        last = first + timedelta(microseconds=1)
    return TimeRange(start=first, end=last)


def _attribute_values(record: ToolRecord) -> dict[str, str]:
    values: dict[str, str] = {}
    for key in KEY_ATTRIBUTES:
        value = getattr(record, key, None)
        if value is not None:
            values[key] = str(value)
    for key, value in getattr(record, "attributes", {}).items():
        if key in KEY_ATTRIBUTES:
            values[key] = str(value)
    return values


def _key_attributes(records: Sequence[ToolRecord], size: int) -> dict[str, tuple[str, ...]]:
    collected: dict[str, set[str]] = {}
    for record in records:
        for key, value in _attribute_values(record).items():
            collected.setdefault(key, set()).add(value)
    return {key: tuple(sorted(values)[:size]) for key, values in sorted(collected.items())}


# per-Tool top values


def _log_top_values(records: Sequence[LogRecord], size: int) -> tuple[TopValue, ...]:
    """``"{level} {service}: {message with digit runs -> #}"`` by count desc, then value."""
    counts: Counter[str] = Counter()
    first_seen: dict[str, datetime] = {}
    for record in records:
        key = f"{record.level} {record.service}: {_normalise_message(record.message)}"
        counts[key] += 1
        if key not in first_seen or record.timestamp < first_seen[key]:
            first_seen[key] = record.timestamp
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:size]
    return tuple(TopValue(value=v, count=c, first_seen=first_seen[v]) for v, c in ordered)


def _metric_top_values(records: Sequence[MetricBucket], size: int) -> tuple[TopValue, ...]:
    """One ``"{metric_name} {service}"`` entry per series, counting its buckets."""
    counts: Counter[str] = Counter()
    first_seen: dict[str, datetime] = {}
    for bucket in records:
        key = f"{bucket.metric_name} {bucket.service}"
        counts[key] += 1
        if key not in first_seen or bucket.timestamp < first_seen[key]:
            first_seen[key] = bucket.timestamp
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:size]
    return tuple(TopValue(value=v, count=c, first_seen=first_seen[v]) for v, c in ordered)


def metric_stats(records: Sequence[MetricBucket]) -> MetricStats | None:
    """Statistics of one series: extremes and their bucket times, point-weighted mean, first and last bucket means."""
    if not records or len({(b.service, b.metric_name) for b in records}) != 1:
        return None
    ordered = sorted(records, key=lambda b: (b.timestamp, b.record_id))
    low = min(ordered, key=lambda b: b.min)  # min() and max() keep the earliest bucket on ties
    high = max(ordered, key=lambda b: b.max)
    total = sum(b.count for b in ordered)
    mean = math.fsum(b.mean * b.count for b in ordered) / total
    return MetricStats(
        min=low.min,
        max=high.max,
        mean=round(mean, 6),
        first=ordered[0].mean,
        last=ordered[-1].mean,
        max_at=high.timestamp,
        min_at=low.timestamp,
    )


def _change_top_values(records: Sequence[ChangeEvent], size: int) -> tuple[TopValue, ...]:
    """``"{change_type} {service} @{ts}: {summary}"``, latest first, then record ID."""
    ordered = sorted(records, key=lambda c: (-c.timestamp.timestamp(), c.record_id))[:size]
    return tuple(
        TopValue(value=f"{c.change_type} {c.service} @{_iso(c.timestamp)}: {c.summary}", count=1, first_seen=c.timestamp)
        for c in ordered
    )


def _dependency_top_values(records: Sequence[DependencyRecord], size: int) -> tuple[TopValue, ...]:
    """``"{direction} {component} ({kind}) hops={n}"`` by hops, then component."""
    entries = sorted(
        ((d.hops, d.component, f"{d.direction} {d.component} ({d.component_kind}) hops={d.hops}") for d in records)
    )[:size]
    return tuple(TopValue(value=value, count=1) for _, _, value in entries)


def _incident_top_values(
    records: Sequence[PastIncident], size: int, query: ToolInputBase | None
) -> tuple[TopValue, ...]:
    """``"{record_id} {date} {title} [cause={cause_type}]"`` by matched-term count desc, then most recent."""
    text = query.query if isinstance(query, SearchIncidentsInput) else None

    def matched(incident: PastIncident) -> int:
        return max(1, matched_term_count(text, incident)) if text else 1

    ordered = sorted(records, key=lambda p: (-matched(p), -p.occurred_at.timestamp(), p.record_id))[:size]
    return tuple(
        TopValue(
            value=f"{p.record_id} {p.occurred_at:%Y-%m-%d} {p.title} [cause={p.cause_type}]",
            count=matched(p),
            first_seen=p.occurred_at,
        )
        for p in ordered
    )


def _generic_top_values(records: Sequence[ToolRecord], size: int) -> tuple[TopValue, ...]:
    """Mixed record kinds (not produced by any single Tool): one entry per kind."""
    counts = Counter(record.kind for record in records)
    return tuple(TopValue(value=kind, count=count) for kind, count in sorted(counts.items())[:size])


class ObservationSummarizer:
    def __init__(self, size: int = 5):
        if not 1 <= size <= 20:
            raise ValueError("Observation_Summary size must be between 1 and 20")
        self.size = size

    def summarize(self, result: ToolResult, query: ToolInputBase | None = None) -> ObservationSummary:
        if result.error is not None:
            return ObservationSummary(record_count=0, note=str(result.error.code))
        records = result.records
        if not records:
            return ObservationSummary(record_count=0, note="no records matched", truncated=result.truncated)
        stats = None
        if all(isinstance(r, LogRecord) for r in records):
            top = _log_top_values(records, self.size)  # type: ignore[arg-type]
        elif all(isinstance(r, MetricBucket) for r in records):
            top = _metric_top_values(records, self.size)  # type: ignore[arg-type]
            stats = metric_stats(records)  # type: ignore[arg-type]
        elif all(isinstance(r, ChangeEvent) for r in records):
            top = _change_top_values(records, self.size)  # type: ignore[arg-type]
        elif all(isinstance(r, DependencyRecord) for r in records):
            top = _dependency_top_values(records, self.size)  # type: ignore[arg-type]
        elif all(isinstance(r, PastIncident) for r in records):
            top = _incident_top_values(records, self.size, query)  # type: ignore[arg-type]
        else:
            top = _generic_top_values(records, self.size)
        return ObservationSummary(
            record_count=len(records),
            time_span=_time_span(records),
            top_values=top,
            key_attributes=_key_attributes(records, self.size),
            metric_stats=stats,
            truncated=result.truncated,
        )
