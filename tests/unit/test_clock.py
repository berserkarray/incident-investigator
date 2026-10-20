"""Injectable_Clock behaviour and the real-sleep guard (Req 30.5)."""

import asyncio
import time

import pytest
from conftest import RealSleepBlocked

from incident_investigator.clock import ClockTimeout, FakeClock, SystemClock


async def test_fake_sleep_advances_virtual_time_without_waiting():
    clock = FakeClock()
    started = time.monotonic()
    await clock.sleep(3600)
    assert clock.monotonic() == 3600
    assert time.monotonic() - started < 1.0


async def test_concurrent_sleepers_wake_in_deadline_order():
    clock = FakeClock()
    woke: list[str] = []

    async def sleeper(name: str, seconds: float):
        await clock.sleep(seconds)
        woke.append(name)

    await asyncio.gather(sleeper("late", 30), sleeper("early", 10), sleeper("mid", 20))
    assert woke == ["early", "mid", "late"]
    assert clock.monotonic() == 30


async def test_wait_for_times_out_on_virtual_deadline():
    clock = FakeClock()
    with pytest.raises(ClockTimeout):
        await clock.wait_for(clock.sleep(100), timeout=5)
    assert clock.monotonic() == 5


async def test_wait_for_returns_result_before_deadline():
    clock = FakeClock()

    async def work():
        await clock.sleep(1)
        return "done"

    assert await clock.wait_for(work(), timeout=5) == "done"


async def test_time_does_not_advance_while_other_work_is_runnable():
    clock = FakeClock()
    timer = asyncio.ensure_future(clock.sleep(10))
    for _ in range(200):  # far more loop turns than the driver's idle threshold
        await asyncio.sleep(0)
        assert clock.monotonic() == 0
    await timer
    assert clock.monotonic() == 10


async def test_manual_advance_without_auto_mode():
    clock = FakeClock(auto_advance=False)
    task = asyncio.ensure_future(clock.sleep(10))
    await asyncio.sleep(0)
    assert not task.done()
    clock.advance(10)
    await asyncio.sleep(0)
    assert task.done()
    assert clock.now().isoformat() == "2024-01-01T00:00:10+00:00"


async def test_real_sleep_is_blocked_in_tests():
    with pytest.raises(RealSleepBlocked):
        await asyncio.sleep(0.01)
    with pytest.raises(RealSleepBlocked):
        time.sleep(0.01)


@pytest.mark.real_clock
async def test_system_clock_wait_for_timeout():
    with pytest.raises(ClockTimeout):
        await SystemClock().wait_for(asyncio.sleep(1), timeout=0.01)
    assert SystemClock().now().tzinfo is not None
