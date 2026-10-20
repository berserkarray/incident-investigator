"""Pure Tool functions over an in-memory snapshot (Req 13.3-13.7, 13.9, 13.12, 13.13).

Each function returns every matching record, sorted by ``(timestamp, record_id)``; the Tool_Layer
applies the record limit and failure injection afterwards. Time ranges are half-open ``[start, end)``.
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from ..models.snapshot import IncidentSnapshot
from ..models.tools import QueryLogsInput, ToolInputBase, ToolRecord, record_timestamp
from ..models.vocab import ToolName

ToolFn = Callable[[ToolInputBase, IncidentSnapshot], list[ToolRecord]]

_MIN_TIME = datetime.min.replace(tzinfo=UTC)


def sort_records(records: Sequence[ToolRecord]) -> list[ToolRecord]:
    return sorted(records, key=lambda r: (record_timestamp(r) or _MIN_TIME, r.record_id))


def cut_to_limit(records: Sequence[ToolRecord], limit: int) -> tuple[tuple[ToolRecord, ...], bool]:
    """The first ``limit`` records and whether any were left out (Req 13.9)."""
    return tuple(records[:limit]), len(records) > limit


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


TOOL_REGISTRY: dict[ToolName, ToolFn] = {
    ToolName.QUERY_LOGS: query_logs,
}
"""Tools available in this build. The remaining four are added in task 5.1."""
