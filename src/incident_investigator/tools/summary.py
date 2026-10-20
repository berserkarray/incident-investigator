"""Observation_Summary: compact, deterministic summaries of Tool_Results (Req 3.2, 3.11, design D3).

A summary depends only on the result, the query and the configured size K. It never contains full
records; Agents that need them look records up on the Blackboard by Evidence_ID.
"""

import re
from collections import Counter
from collections.abc import Sequence
from datetime import datetime, timedelta

from ..models.base import TimeRange
from ..models.evidence import ObservationSummary, TopValue
from ..models.snapshot import LogRecord
from ..models.tools import ToolRecord, ToolResult, record_timestamp

KEY_ATTRIBUTES = ("service", "level", "metric_name", "change_type", "cohort", "monitor", "status", "component", "direction")
_DIGITS = re.compile(r"\d+")


def _normalise_message(message: str) -> str:
    return _DIGITS.sub("#", message)


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


def _log_top_values(records: Sequence[LogRecord], size: int) -> tuple[TopValue, ...]:
    counts: Counter[str] = Counter()
    first_seen: dict[str, datetime] = {}
    for record in records:
        key = f"{record.level} {record.service}: {_normalise_message(record.message)}"
        counts[key] += 1
        if key not in first_seen or record.timestamp < first_seen[key]:
            first_seen[key] = record.timestamp
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:size]
    return tuple(TopValue(value=v, count=c, first_seen=first_seen[v]) for v, c in ordered)


def _generic_top_values(records: Sequence[ToolRecord], size: int) -> tuple[TopValue, ...]:
    """Fallback for record kinds whose per-Tool rule is added in task 5.1: one entry per kind."""
    counts = Counter(record.kind for record in records)
    return tuple(TopValue(value=kind, count=count) for kind, count in sorted(counts.items())[:size])


class ObservationSummarizer:
    def __init__(self, size: int = 5):
        if not 1 <= size <= 20:
            raise ValueError("Observation_Summary size must be between 1 and 20")
        self.size = size

    def summarize(self, result: ToolResult) -> ObservationSummary:
        if result.error is not None:
            return ObservationSummary(record_count=0, note=str(result.error.code))
        records = result.records
        if not records:
            return ObservationSummary(record_count=0, note="no records matched", truncated=result.truncated)
        if all(isinstance(r, LogRecord) for r in records):
            top = _log_top_values(records, self.size)  # type: ignore[arg-type]
        else:
            top = _generic_top_values(records, self.size)
        return ObservationSummary(
            record_count=len(records),
            time_span=_time_span(records),
            top_values=top,
            key_attributes=_key_attributes(records, self.size),
            truncated=result.truncated,
        )
