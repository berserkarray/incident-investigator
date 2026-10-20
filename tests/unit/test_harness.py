"""The suite runs offline with no API key (Req 30.1), and secrets never reach traces (Req 5.10)."""

import os
import socket

import pytest
from conftest import NetworkBlocked

from incident_investigator.clock import FakeClock
from incident_investigator.models.vocab import AgentName, SystemName
from incident_investigator.redact import API_KEY_ENV_VARS, SecretRedactor
from incident_investigator.tracing import TraceRecorder


def test_api_keys_are_unset():
    for name in API_KEY_ENV_VARS:
        assert name not in os.environ


def test_network_is_blocked():
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("example.com", 443))
    with pytest.raises(NetworkBlocked):
        socket.socket().connect(("127.0.0.1", 9))


def test_redactor_replaces_every_secret():
    redactor = SecretRedactor.from_env({"OPENAI_API_KEY": "sk-test-123456", "ANTHROPIC_API_KEY": ""})
    assert redactor.redact("key=sk-test-123456!") == "key=[REDACTED]!"
    assert redactor.redact_obj({"a": ["sk-test-123456"]}) == {"a": ["[REDACTED]"]}
    assert not SecretRedactor().active


def test_trace_recorder_sequences_and_redacts():
    recorder = TraceRecorder("run-1", SystemName.MULTI, FakeClock(), SecretRedactor(["sk-secret-value"]))
    recorder.record(
        "agent_failure",
        agent=AgentName.CRITIC,
        task_id="T5",
        attempt=1,
        failure_kind="exception",
        message="auth failed for sk-secret-value",
    )
    recorder.record("task_transition", task_id="T5", agent="critic", from_state="pending", to_state="running")
    trace = recorder.to_trace()
    assert [e.seq for e in trace.events] == [0, 1]
    assert "sk-secret-value" not in trace.model_dump_json()
    assert trace.of_type("agent_failure")[0].message.endswith("[REDACTED]")
