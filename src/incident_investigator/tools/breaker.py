"""Circuit_Breaker: per-Tool closed/open state with one Trial_Call after the cooldown (Req 15.4-15.8).

Time values come from the caller, which reads them from the Injectable_Clock (Req 15.11).
"""

from dataclasses import dataclass
from enum import StrEnum, auto

from ..models.config import CircuitBreakerSettings
from ..models.vocab import ToolName


class Admission(StrEnum):
    NORMAL = auto()
    TRIAL = auto()
    REJECT = auto()


@dataclass
class BreakerState:
    is_open: bool = False
    consecutive_failures: int = 0
    opened_at: float | None = None
    trial_in_flight: bool = False


class CircuitBreaker:
    """One instance per Run; every Tool starts closed with a Consecutive_Failure_Count of 0."""

    def __init__(self, failure_threshold: int, cooldown_s: float):
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1")
        if cooldown_s <= 0:
            raise ValueError("cooldown_s must be positive")
        self.failure_threshold = failure_threshold
        self.cooldown_s = cooldown_s
        self._states: dict[ToolName, BreakerState] = {}

    @classmethod
    def from_settings(cls, settings: CircuitBreakerSettings) -> "CircuitBreaker":
        return cls(settings.failure_threshold, settings.cooldown_s)

    def _state(self, tool: ToolName) -> BreakerState:
        return self._states.setdefault(ToolName(tool), BreakerState())

    def state(self, tool: ToolName) -> BreakerState:
        """A copy of the current state of ``tool``."""
        s = self._state(tool)
        return BreakerState(s.is_open, s.consecutive_failures, s.opened_at, s.trial_in_flight)

    def admit(self, tool: ToolName, now: float) -> Admission:
        s = self._state(tool)
        if not s.is_open:
            return Admission.NORMAL
        assert s.opened_at is not None
        if now - s.opened_at >= self.cooldown_s and not s.trial_in_flight:  # Req 15.6
            s.trial_in_flight = True
            return Admission.TRIAL
        return Admission.REJECT  # Req 15.5

    def record(self, tool: ToolName, admission: Admission, timed_out: bool, now: float) -> None:
        """Record the return of an admitted Tool_Call; ``now`` is when it returned."""
        s = self._state(tool)
        if admission is Admission.REJECT:
            return
        if admission is Admission.TRIAL:
            s.trial_in_flight = False
            if timed_out:  # Req 15.4: a timed-out trial keeps the breaker open and restarts the cooldown
                s.consecutive_failures += 1
                s.opened_at = now
            else:  # Req 15.7
                s.is_open = False
                s.consecutive_failures = 0
                s.opened_at = None
            return
        if timed_out:
            s.consecutive_failures += 1
            if s.consecutive_failures >= self.failure_threshold:  # Req 15.4: open, or stay open
                s.is_open = True
                s.opened_at = now
        else:
            # A non-trial success resets the count; it does not close an open breaker.
            s.consecutive_failures = 0

    def release_trial(self, tool: ToolName) -> None:
        """The trial was admitted but never made (for example, the Tool_Call Budget refused it)."""
        self._state(tool).trial_in_flight = False
