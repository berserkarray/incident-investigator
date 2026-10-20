"""Selects the LLM client named in the configuration (Req 5.2, 5.9, 5.11).

- ``fake``: the OfflineResponder. It never reads the environment and never fails, so it is not wrapped
  in the retrying client.
- ``openai``: the OpenAI adapter inside ``RetryingLLMClient``. The key comes only from
  ``OPENAI_API_KEY`` in ``env``.
- ``anthropic``: the adapter is optional (task 7.9) and not built in this delivery.
"""

from collections.abc import Mapping

from ..clock import Clock
from ..models.config import LLMSettings
from .base import LLMClient
from .offline import OfflineResponder
from .retrying import RetryingLLMClient

API_KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


class ProviderUnavailable(Exception):
    pass


def build_llm_client(settings: LLMSettings, clock: Clock, env: Mapping[str, str]) -> LLMClient:
    if settings.provider == "fake":
        return OfflineResponder()
    if settings.provider == "openai":
        api_key = env.get(API_KEY_ENV["openai"], "")
        if not api_key.strip():
            raise ProviderUnavailable(
                f"provider openai needs the environment variable {API_KEY_ENV['openai']}, which is not set"
            )
        from .openai import OpenAIAdapter  # the SDK is imported only when the provider is used

        return RetryingLLMClient(OpenAIAdapter(api_key, timeout_s=settings.request_timeout_s), clock, settings)
    raise ProviderUnavailable(
        f"provider {settings.provider!r} is not built in this delivery (the Anthropic adapter is optional); "
        'use provider = "openai" or "fake"'
    )
