"""Base model, constrained string types, time ranges and canonical JSON."""

import json
from datetime import datetime, timedelta
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    JsonValue,
    StringConstraints,
    model_validator,
)

__all__ = [
    "AwareDatetime",
    "IsoAwareDatetime",
    "JsonValue",
    "NonBlank",
    "NonBlank100",
    "NonBlank200",
    "NonBlank500",
    "NonBlank1000",
    "StrictModel",
    "TimeRange",
    "canonical_json",
]


class StrictModel(BaseModel):
    """Immutable model that rejects unknown fields. Updates use ``model_copy(update=...)``."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _not_blank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


def NonBlank(max_length: int) -> Any:  # noqa: N802 - reads like a type in annotations
    """A string whose stripped length is at least 1 and whose raw length is at most ``max_length``."""
    return Annotated[str, StringConstraints(max_length=max_length), AfterValidator(_not_blank)]


NonBlank100 = NonBlank(100)
NonBlank200 = NonBlank(200)
NonBlank500 = NonBlank(500)
NonBlank1000 = NonBlank(1000)


def _iso_string_or_datetime(value: Any) -> Any:
    """Accept ISO 8601 strings and datetime objects only (no epoch numbers)."""
    if isinstance(value, (str, datetime)):
        return value
    raise ValueError("must be an ISO 8601 timestamp string with an explicit UTC offset")


IsoAwareDatetime = Annotated[AwareDatetime, BeforeValidator(_iso_string_or_datetime)]


class TimeRange(StrictModel):
    """Half-open time range ``[start, end)``; ``start`` must be before ``end``."""

    start: AwareDatetime
    end: AwareDatetime

    @model_validator(mode="after")
    def _start_before_end(self) -> "TimeRange":
        if not self.start < self.end:
            raise ValueError("time range start must be before end")
        return self

    def contains(self, moment: datetime) -> bool:
        return self.start <= moment < self.end

    @property
    def duration(self) -> timedelta:
        return self.end - self.start


def canonical_json(value: Any) -> str:
    """Deterministic JSON: models dumped in JSON mode, keys sorted, no whitespace."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
