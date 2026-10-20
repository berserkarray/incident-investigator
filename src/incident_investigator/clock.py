"""Injectable_Clock: every time read and sleep goes through a Clock (Req 15.11, 30.5)."""

import asyncio
import heapq
import itertools
import time
from collections.abc import Awaitable
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Protocol, TypeVar

T = TypeVar("T")


class ClockTimeout(Exception):
    """Raised by ``Clock.wait_for`` when the awaited work does not finish in time."""

    def __init__(self, timeout: float):
        super().__init__(f"timed out after {timeout} s")
        self.timeout = timeout


class Clock(Protocol):
    def now(self) -> datetime:
        """Aware UTC wall-clock time."""

    def monotonic(self) -> float:
        """Seconds for measuring durations."""

    async def sleep(self, seconds: float) -> None: ...

    async def wait_for(self, aw: Awaitable[T], timeout: float) -> T:
        """Await ``aw``; cancel it and raise ``ClockTimeout`` after ``timeout`` seconds."""


class SystemClock:
    """The real clock, used outside tests."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))

    async def wait_for(self, aw: Awaitable[T], timeout: float) -> T:
        try:
            return await asyncio.wait_for(aw, timeout)
        except TimeoutError:
            raise ClockTimeout(timeout) from None


DEFAULT_FAKE_START = datetime(2024, 1, 1, tzinfo=UTC)


class FakeClock:
    """Virtual time for tests. Never waits on the real clock.

    ``advance(dt)`` moves time forward and wakes every sleeper whose deadline has passed.
    With ``auto_advance=True`` a driver task jumps to the next deadline once the event loop
    has gone ``idle_turns`` turns without new sleeps being registered, so code that sleeps
    or times out runs to completion without manual advancing.
    """

    def __init__(self, start: datetime = DEFAULT_FAKE_START, *, auto_advance: bool = True, idle_turns: int = 20):
        if start.tzinfo is None:
            raise ValueError("FakeClock start must be timezone-aware")
        self._start = start
        self._elapsed = 0.0
        self._sleepers: list[tuple[float, int, asyncio.Future]] = []
        self._order = itertools.count()
        self._auto_advance = auto_advance
        self._idle_turns = idle_turns
        self._activity = 0
        self._driver: asyncio.Task | None = None

    def now(self) -> datetime:
        return self._start + timedelta(seconds=self._elapsed)

    def monotonic(self) -> float:
        return self._elapsed

    def advance(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("cannot move time backwards")
        self._elapsed += seconds
        self._wake_due()

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        future = asyncio.get_running_loop().create_future()
        heapq.heappush(self._sleepers, (self._elapsed + seconds, next(self._order), future))
        self._activity += 1
        self._ensure_driver()
        await future

    async def wait_for(self, aw: Awaitable[T], timeout: float) -> T:
        work = asyncio.ensure_future(aw)
        timer = asyncio.ensure_future(self.sleep(timeout))
        try:
            await asyncio.wait({work, timer}, return_when=asyncio.FIRST_COMPLETED)
            if work.done():
                return work.result()
            work.cancel()
            with suppress(asyncio.CancelledError):
                await work
            raise ClockTimeout(timeout)
        finally:
            timer.cancel()
            if not work.done():
                work.cancel()

    # internals

    def _prune(self) -> None:
        while self._sleepers and self._sleepers[0][2].done():
            heapq.heappop(self._sleepers)

    def _wake_due(self) -> None:
        self._prune()
        while self._sleepers and self._sleepers[0][0] <= self._elapsed:
            _, _, future = heapq.heappop(self._sleepers)
            if not future.done():
                future.set_result(None)
            self._prune()

    def _ensure_driver(self) -> None:
        if self._auto_advance and (self._driver is None or self._driver.done()):
            self._driver = asyncio.get_running_loop().create_task(self._drive())

    async def _drive(self) -> None:
        idle = 0
        seen = self._activity
        while True:
            self._prune()
            if not self._sleepers:
                return
            await asyncio.sleep(0)
            if self._activity != seen:
                seen = self._activity
                idle = 0
                continue
            idle += 1
            if idle >= self._idle_turns:
                self._prune()
                if self._sleepers:
                    self._elapsed = max(self._elapsed, self._sleepers[0][0])
                    self._wake_due()
                idle = 0
