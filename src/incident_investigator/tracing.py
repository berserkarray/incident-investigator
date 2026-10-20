"""TraceRecorder: assigns sequence numbers, stamps Clock time and redacts secrets (Req 29.3-29.6)."""

from typing import Any

from .clock import Clock
from .models.trace import TRACE_EVENT_ADAPTER, RunTrace, TraceEvent
from .models.vocab import SystemName
from .redact import SecretRedactor


class TraceRecorder:
    def __init__(self, run_id: str, system: SystemName, clock: Clock, redactor: SecretRedactor | None = None):
        self.run_id = run_id
        self.system = system
        self._clock = clock
        self._redactor = redactor or SecretRedactor()
        self._events: list[TraceEvent] = []

    def record(self, event_type: str, **fields: Any) -> TraceEvent:
        """Validate and append one event of ``event_type`` (the event's ``type`` discriminator)."""
        payload = {"type": event_type, "seq": len(self._events), "at": self._clock.now(), **fields}
        if self._redactor.active:
            payload = TRACE_EVENT_ADAPTER.validate_python(payload).model_dump(mode="json")
            payload = self._redactor.redact_obj(payload)
        event = TRACE_EVENT_ADAPTER.validate_python(payload)
        self._events.append(event)
        return event

    @property
    def events(self) -> tuple[TraceEvent, ...]:
        return tuple(self._events)

    def to_trace(self) -> RunTrace:
        return RunTrace(run_id=self.run_id, system=self.system, events=tuple(self._events))
