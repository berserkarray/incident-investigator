"""Test-suite guards: no network, no API keys, no real sleeps (Req 30.1, 30.5).

Tests that exercise ``SystemClock`` opt out of the sleep guard with ``@pytest.mark.real_clock``.
"""

import asyncio
import os
import socket
import time

import pytest
from hypothesis import HealthCheck, settings

from incident_investigator.redact import API_KEY_ENV_VARS

settings.register_profile(
    "ci",
    max_examples=25,
    derandomize=True,
    database=None,
    deadline=None,
    print_blob=True,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))


class NetworkBlocked(RuntimeError):
    pass


class RealSleepBlocked(RuntimeError):
    pass


_REAL_ASYNCIO_SLEEP = asyncio.sleep
_REAL_TIME_SLEEP = time.sleep
_state = {"allow_real_sleep": False}


async def _guarded_asyncio_sleep(delay, result=None):
    if delay and delay > 0 and not _state["allow_real_sleep"]:
        raise RealSleepBlocked(f"asyncio.sleep({delay}) on the real clock; use FakeClock")
    return await _REAL_ASYNCIO_SLEEP(delay, result)


def _guarded_time_sleep(seconds):
    if seconds and seconds > 0 and not _state["allow_real_sleep"]:
        raise RealSleepBlocked(f"time.sleep({seconds}) on the real clock; use FakeClock")
    return _REAL_TIME_SLEEP(seconds)


def _blocked(*args, **kwargs):
    raise NetworkBlocked("network access is blocked in the test suite")


@pytest.fixture(scope="session", autouse=True)
def _session_guards():
    with pytest.MonkeyPatch.context() as mp:
        for name in API_KEY_ENV_VARS:
            mp.delenv(name, raising=False)
        mp.setattr(socket.socket, "connect", _blocked)
        mp.setattr(socket.socket, "connect_ex", _blocked)
        mp.setattr(socket, "create_connection", _blocked)
        mp.setattr(socket, "getaddrinfo", _blocked)
        mp.setattr(asyncio, "sleep", _guarded_asyncio_sleep)
        mp.setattr(time, "sleep", _guarded_time_sleep)
        yield


@pytest.fixture(autouse=True)
def _per_test_guards(request, monkeypatch):
    for name in API_KEY_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    _state["allow_real_sleep"] = request.node.get_closest_marker("real_clock") is not None
    yield
    _state["allow_real_sleep"] = False
