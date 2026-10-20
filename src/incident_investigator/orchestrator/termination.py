"""TerminationArbiter: records exactly one Termination_Reason per Run (Req 18.7, design D13).

Conditions are raised by ``signal()`` at the check points: after each LLM usage record (gateway),
after each Tool_Result (Tool_Layer), after each Task terminal transition or Critic decision
(scheduler and critic loop) and when the watchdog fires. Two conditions are also read as state at
every check: Token_Usage against the token Budget minus the reserve, and elapsed time against the
wall-clock timeout. At a check, the first condition in Req 18.7 order wins; later checks change nothing.
"""

import asyncio
from collections.abc import Callable
from enum import StrEnum, auto

from ..models.vocab import INVESTIGATOR_REASONS, TerminationReason

PRECEDENCE: tuple[TerminationReason, ...] = (
    TerminationReason.CRITIC_ACCEPTED,
    TerminationReason.MAX_CRITIC_ROUNDS,
    TerminationReason.DUPLICATE_REQUESTS_ONLY,
    TerminationReason.TOOL_CALL_BUDGET_EXHAUSTED,
    TerminationReason.TOKEN_BUDGET_EXHAUSTED,
    TerminationReason.WALL_CLOCK_TIMEOUT,
    TerminationReason.CRITIC_FAILED,
    TerminationReason.NO_HYPOTHESES,
)
"""Req 18.7 order: the earliest-listed condition wins when several hold at the same check."""


class CheckPoint(StrEnum):
    LLM_USAGE = auto()
    TOOL_RESULT = auto()
    TASK_TERMINAL = auto()
    CRITIC_DECISION = auto()
    WATCHDOG = auto()
    OTHER = auto()


class TerminationArbiter:
    def __init__(
        self,
        *,
        tokens_used: Callable[[], int],
        token_threshold: int,
        elapsed: Callable[[], float],
        wall_clock_s: float,
        on_end: Callable[[TerminationReason], None] | None = None,
    ):
        """``token_threshold`` is the token Budget minus the Reporting_Agent reserve. ``on_end`` runs
        synchronously once, when the reason is recorded, so the loop stops before anything else runs."""
        self._tokens_used = tokens_used
        self._token_threshold = token_threshold
        self._elapsed = elapsed
        self._wall_clock_s = wall_clock_s
        self._on_end = on_end
        self._raised: set[TerminationReason] = set()
        self._reason: TerminationReason | None = None
        self._point: CheckPoint | None = None
        self._ended = asyncio.Event()

    @property
    def reason(self) -> TerminationReason | None:
        return self._reason

    @property
    def point(self) -> CheckPoint | None:
        """The check point at which the reason was recorded."""
        return self._point

    @property
    def ended(self) -> bool:
        return self._reason is not None

    async def wait(self) -> TerminationReason:
        await self._ended.wait()
        assert self._reason is not None
        return self._reason

    def signal(self, reason: TerminationReason, *, point: CheckPoint = CheckPoint.OTHER, check: bool = True) -> None:
        """Raise a condition. With ``check=False`` the condition waits for the next ``check()``."""
        if reason not in INVESTIGATOR_REASONS:
            raise ValueError(f"{reason} is not an Investigator Termination_Reason")
        self._raised.add(reason)
        if check:
            self.check(point)

    def _holds(self) -> set[TerminationReason]:
        holding = set(self._raised)
        if self._tokens_used() >= self._token_threshold:
            holding.add(TerminationReason.TOKEN_BUDGET_EXHAUSTED)
        if self._elapsed() >= self._wall_clock_s:
            holding.add(TerminationReason.WALL_CLOCK_TIMEOUT)
        return holding

    def check(self, point: CheckPoint = CheckPoint.OTHER) -> TerminationReason | None:
        """Record the first holding condition in Req 18.7 order, once. Returns the recorded reason."""
        if self._reason is not None:
            return self._reason
        holding = self._holds()
        for reason in PRECEDENCE:
            if reason in holding:
                self._reason = reason
                self._point = point
                self._ended.set()
                if self._on_end is not None:
                    self._on_end(reason)
                return reason
        return None
