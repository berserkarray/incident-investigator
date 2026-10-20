"""LLM client stack: retry schedule, OpenAI adapter request shape and error mapping, client selection
(task 7.8; example tests from 7.10). No network: the OpenAI SDK runs over an in-memory transport."""

import json

import httpx2
import pytest
from factories import make_incident_report

from incident_investigator.clock import FakeClock
from incident_investigator.llm.base import LLMCallError, LLMErrorCategory, LLMRequest
from incident_investigator.llm.factory import ProviderUnavailable, build_llm_client
from incident_investigator.llm.fake import ScriptedFakeLLM, ScriptEntry
from incident_investigator.llm.gateway import LLMGateway
from incident_investigator.llm.offline import OfflineResponder
from incident_investigator.llm.openai import OpenAIAdapter
from incident_investigator.llm.retrying import RetryingLLMClient
from incident_investigator.models.budget import BudgetLimits
from incident_investigator.models.config import LLMSettings
from incident_investigator.models.vocab import AgentName, CallKind, SystemName
from incident_investigator.state.blackboard import Blackboard
from incident_investigator.tracing import TraceRecorder

FAKE_KEY = "sk-test-0000000000000000-not-a-real-key"
SETTINGS = LLMSettings(
    provider="openai", model="gpt-5.4-mini", request_timeout_s=60, max_attempts=3, base_delay_s=1.0, max_delay_s=1.5
)


def request(**overrides) -> LLMRequest:
    data = {
        "caller": AgentName.CRITIC,
        "call_kind": CallKind.CRITIC,
        "model": "gpt-5.4-mini",
        "system": "Answer with one JSON object only.",
        "user": "<context>\n{}\n</context>",
    }
    return LLMRequest(**{**data, **overrides})


# RetryingLLMClient (Req 5.7, 5.8)


@pytest.mark.parametrize("category", [LLMErrorCategory.RATE_LIMIT, LLMErrorCategory.SERVER])
async def test_retryable_errors_back_off_on_the_clock_and_then_succeed(category):
    clock = FakeClock()
    fake = ScriptedFakeLLM(
        [ScriptEntry(AgentName.CRITIC, raise_category=category)] * 2 + [ScriptEntry(AgentName.CRITIC, text="{}")], clock
    )
    response = await RetryingLLMClient(fake, clock, SETTINGS).complete(request())
    assert response.text == "{}" and len(fake.prompts) == 3
    assert clock.monotonic() == 1.0 + 1.5  # base 1.0, then 2.0 capped at max_delay 1.5
    # the retries stay visible: attempts and the failed attempts' categories travel with the response
    assert response.attempts == 3 and response.retried_errors == (str(category), str(category))


async def test_a_retried_call_is_traced_with_its_attempts():
    clock = FakeClock()
    trace = TraceRecorder("r", SystemName.MULTI, clock)
    board = Blackboard("r", "inc", make_incident_report(), clock, trace)
    fake = ScriptedFakeLLM(
        [ScriptEntry(AgentName.CRITIC, raise_category=LLMErrorCategory.RATE_LIMIT), ScriptEntry(AgentName.CRITIC, text="{}")],
        clock,
    )
    limits = BudgetLimits(critic_rounds=3, tool_calls=10, tokens=100_000, wall_clock_s=60, reporting_reserve=0)
    gateway = LLMGateway(RetryingLLMClient(fake, clock, SETTINGS), board, trace, limits, "m")
    await gateway.raw(request())
    (event,) = trace.to_trace().of_type("llm_call")
    assert (event.attempt, event.outcome, event.retried_errors) == (2, "ok", ("rate_limit",))


async def test_last_attempt_failure_names_the_category_and_attempts():
    clock = FakeClock()
    fake = ScriptedFakeLLM([ScriptEntry(AgentName.CRITIC, raise_category=LLMErrorCategory.SERVER)] * 3, clock)
    with pytest.raises(LLMCallError, match="server") as caught:
        await RetryingLLMClient(fake, clock, SETTINGS).complete(request())
    assert caught.value.category is LLMErrorCategory.SERVER and caught.value.attempts == 3


@pytest.mark.parametrize("category", [LLMErrorCategory.AUTH, LLMErrorCategory.BAD_REQUEST, LLMErrorCategory.OTHER])
async def test_other_errors_are_not_retried(category):
    clock = FakeClock()
    fake = ScriptedFakeLLM([ScriptEntry(AgentName.CRITIC, raise_category=category)] * 3, clock)
    with pytest.raises(LLMCallError) as caught:
        await RetryingLLMClient(fake, clock, SETTINGS).complete(request())
    assert caught.value.category is category and caught.value.attempts == 1 and len(fake.prompts) == 1
    assert clock.monotonic() == 0


async def test_an_attempt_past_the_request_timeout_is_a_retried_timeout():
    clock = FakeClock()
    fake = ScriptedFakeLLM(
        [ScriptEntry(AgentName.CRITIC, text="{}", delay_s=120), ScriptEntry(AgentName.CRITIC, text='{"second": 1}')], clock
    )
    response = await RetryingLLMClient(fake, clock, SETTINGS).complete(request())
    assert response.text == '{"second": 1}'
    assert clock.monotonic() == 60 + 1.0  # the timed-out attempt, then the first backoff


# OpenAI adapter (Req 5.1-5.3, 5.8, 5.10) over an in-memory transport


def responses_body(text: str, input_tokens: int = 11, output_tokens: int = 7) -> dict:
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 0,
        "model": "gpt-5.4-mini",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
        ],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
            "input_tokens_details": {"cached_tokens": 0},
            "output_tokens_details": {"reasoning_tokens": 0},
        },
    }


def adapter(handler) -> tuple[OpenAIAdapter, list[httpx2.Request]]:
    seen: list[httpx2.Request] = []

    def record(req: httpx2.Request) -> httpx2.Response:
        seen.append(req)
        return handler(req)

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(record))
    return OpenAIAdapter(FAKE_KEY, timeout_s=60, http_client=client), seen


async def test_openai_adapter_request_shape_and_usage_mapping():
    llm, seen = adapter(lambda req: httpx2.Response(200, json=responses_body('{"accept_hypothesis_id": "H1"}')))
    response = await llm.complete(request(max_output_tokens=2048))
    assert response.text == '{"accept_hypothesis_id": "H1"}' and response.provider == "openai"
    assert (response.usage.input_tokens, response.usage.output_tokens) == (11, 7)
    (sent,) = seen
    assert sent.method == "POST" and sent.url.path.endswith("/responses")
    assert sent.headers["authorization"] == f"Bearer {FAKE_KEY}"
    body = json.loads(sent.content)
    assert body == {
        "model": "gpt-5.4-mini",
        "instructions": "Answer with one JSON object only.",
        "input": "<context>\n{}\n</context>",
        "max_output_tokens": 2048,
        "reasoning": {"effort": "none"},
        "text": {"format": {"type": "json_object"}},
        "store": False,
        "temperature": 0.0,
    }


async def test_openai_adapter_omits_temperature_when_the_request_has_none():
    llm, seen = adapter(lambda req: httpx2.Response(200, json=responses_body("{}")))
    await llm.complete(request(temperature=None))
    assert "temperature" not in json.loads(seen[0].content)


@pytest.mark.parametrize(
    ("status", "category"),
    [
        (429, LLMErrorCategory.RATE_LIMIT),
        (500, LLMErrorCategory.SERVER),
        (503, LLMErrorCategory.SERVER),
        (401, LLMErrorCategory.AUTH),
        (403, LLMErrorCategory.AUTH),
        (400, LLMErrorCategory.BAD_REQUEST),
        (404, LLMErrorCategory.BAD_REQUEST),
        (408, LLMErrorCategory.TIMEOUT),
    ],
)
async def test_openai_errors_map_to_categories_with_the_key_redacted(status, category):
    body = {"error": {"message": f"problem with key {FAKE_KEY}", "type": "error", "code": None}}
    llm, seen = adapter(lambda req: httpx2.Response(status, json=body))
    with pytest.raises(LLMCallError) as caught:
        await llm.complete(request())
    assert caught.value.category is category and len(seen) == 1  # max_retries=0: no SDK retry
    assert FAKE_KEY not in str(caught.value) and "[REDACTED]" in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__suppress_context__


async def test_openai_connection_failure_is_a_retryable_server_error():
    def fail(req):
        raise httpx2.ConnectError("connection refused")

    llm, _ = adapter(fail)
    with pytest.raises(LLMCallError) as caught:
        await llm.complete(request())
    assert caught.value.category is LLMErrorCategory.SERVER


# client selection (Req 5.2, 5.9, 5.11)


def test_build_llm_client_selects_the_configured_provider():
    clock = FakeClock()
    fake = build_llm_client(LLMSettings(provider="fake", model="offline"), clock, {"OPENAI_API_KEY": FAKE_KEY})
    assert isinstance(fake, OfflineResponder)
    with pytest.raises(ProviderUnavailable, match="OPENAI_API_KEY"):
        build_llm_client(SETTINGS, clock, {})
    real = build_llm_client(SETTINGS, clock, {"OPENAI_API_KEY": FAKE_KEY})
    assert isinstance(real, RetryingLLMClient) and isinstance(real.inner, OpenAIAdapter)
    with pytest.raises(ProviderUnavailable, match="not built"):
        build_llm_client(LLMSettings(provider="anthropic", model="m"), clock, {"ANTHROPIC_API_KEY": "x"})
