"""OpenAI Provider_Adapter (Req 5.1-5.3, 5.8-5.10), written for the ``openai`` 3.x SDK.

Request shape, checked against the OpenAI documentation for ``gpt-5.4-mini`` (October 2026; no API
call was made to check it). Sources: https://developers.openai.com/api/docs/models/gpt-5.4-mini,
https://developers.openai.com/api/docs/guides/latest-model/,
https://community.openai.com/t/gpt-5-4-ignores-reasoning-effort-none-when-max-completion-tokens-is-used/1378362
and https://github.com/openai/openai-python/issues/3073.

- Endpoint: the Responses API (``client.responses.create``). The model serves both Chat Completions
  and Responses, and OpenAI recommends Responses for its GPT-5 reasoning models. On Chat Completions,
  GPT-5.4 models reject ``max_tokens`` (``max_completion_tokens`` replaces it), and combining
  ``max_completion_tokens`` with ``reasoning_effort="none"`` was reported to spend the whole cap on
  reasoning tokens; Responses with ``max_output_tokens`` does not have that problem.
- ``reasoning.effort = "none"``, the model's default: lowest latency and cost per call. The
  investigation's own Critic loop does the deliberate checking.
- ``temperature`` is accepted by GPT-5.4 models only while reasoning effort is ``none``, so it is sent
  only in that case (``LLMRequest.temperature``, 0.0 by default; None omits it).
- ``max_output_tokens = LLMRequest.max_output_tokens`` caps visible output plus any reasoning tokens.
- JSON mode, ``text.format = {"type": "json_object"}``. Every structured prompt contains the word
  JSON and the output JSON Schema; the Structured_Output_Validator enforces the schema with one repair.
  Strict ``json_schema`` mode cannot express parts of the Pydantic schemas (open ``dict`` fields,
  defaults), so it is not used.
- ``store = False``: prompts and outputs are not kept for later retrieval.
- Usage: ``response.usage.input_tokens`` / ``output_tokens`` (the Responses names for Chat
  Completions' ``prompt_tokens`` / ``completion_tokens``) map to ``TokenCount`` (Req 5.3).

The SDK client is built with ``max_retries=0``; retries and backoff happen in ``RetryingLLMClient`` on
the Injectable_Clock (Req 5.7). SDK errors map to ``LLMErrorCategory``; messages are redacted and
the SDK exception is not chained, because it carries the HTTP request (Req 5.10).
"""

import time
from typing import Any

import openai
from openai import AsyncOpenAI

from ..models.budget import TokenCount
from ..redact import SecretRedactor
from .base import LLMCallError, LLMErrorCategory, LLMRequest, LLMResponse

REASONING_EFFORT = "none"
"""gpt-5.4-mini supports none (default), low, medium, high and xhigh."""

MAX_ERROR_MESSAGE = 500


def request_params(request: LLMRequest) -> dict[str, Any]:
    """The keyword arguments of ``responses.create`` for one LLMRequest."""
    params: dict[str, Any] = {
        "model": request.model,
        "instructions": request.system,
        "input": request.user,
        "max_output_tokens": request.max_output_tokens,
        "reasoning": {"effort": REASONING_EFFORT},
        "text": {"format": {"type": "json_object"}},
        "store": False,
    }
    if request.temperature is not None and REASONING_EFFORT == "none":
        params["temperature"] = request.temperature
    return params


def error_category(exc: Exception) -> LLMErrorCategory:
    """Map an SDK exception to the provider error category (Req 5.7, 5.8)."""
    if isinstance(exc, openai.RateLimitError):
        return LLMErrorCategory.RATE_LIMIT
    if isinstance(exc, openai.APITimeoutError):  # before APIConnectionError, its base class
        return LLMErrorCategory.TIMEOUT
    if isinstance(exc, openai.APIConnectionError):
        return LLMErrorCategory.SERVER  # provider unreachable: transient, retried like a 5xx
    if isinstance(exc, (openai.AuthenticationError, openai.PermissionDeniedError)):
        return LLMErrorCategory.AUTH
    if isinstance(exc, openai.APIStatusError):
        if exc.status_code == 408:
            return LLMErrorCategory.TIMEOUT
        if exc.status_code >= 500:
            return LLMErrorCategory.SERVER
        if 400 <= exc.status_code < 500:
            return LLMErrorCategory.BAD_REQUEST
    return LLMErrorCategory.OTHER


def _describe(exc: Exception) -> str:
    status = getattr(exc, "status_code", None)
    message = getattr(exc, "message", None) or str(exc)
    head = f"{type(exc).__name__}" + (f" (HTTP {status})" if status else "")
    return f"{head}: {message}"[:MAX_ERROR_MESSAGE]


class OpenAIAdapter:
    """``LLMClient`` over the OpenAI Responses API. One instance serves every call of a Run."""

    provider = "openai"

    def __init__(self, api_key: str, *, timeout_s: float, http_client: Any | None = None):
        # build_llm_client passes the key from OPENAI_API_KEY (Req 5.9); it is never logged, and any
        # occurrence in an SDK error message is redacted.
        self._redactor = SecretRedactor([api_key])
        self._client = AsyncOpenAI(api_key=api_key, max_retries=0, timeout=timeout_s, http_client=http_client)

    async def complete(self, request: LLMRequest) -> LLMResponse:
        started = time.perf_counter()
        try:
            response = await self._client.responses.create(**request_params(request))
        except openai.OpenAIError as exc:
            raise LLMCallError(error_category(exc), self._redactor.redact(_describe(exc))) from None
        usage = response.usage
        tokens = TokenCount(
            input_tokens=usage.input_tokens if usage is not None else 0,
            output_tokens=usage.output_tokens if usage is not None else 0,
        )
        return LLMResponse(
            text=response.output_text,
            usage=tokens,
            provider="openai",
            duration_s=time.perf_counter() - started,
        )

    async def close(self) -> None:
        await self._client.close()
