"""Selects the LLM client named in the configuration (Req 5.2, 5.9, 5.11)."""

from collections.abc import Mapping

from ..clock import Clock
from ..models.config import LLMSettings
from .base import LLMClient
from .offline import OfflineResponder

API_KEY_ENV = {"openai": "OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}


class ProviderUnavailable(Exception):
    pass


def build_llm_client(settings: LLMSettings, clock: Clock, env: Mapping[str, str]) -> LLMClient:
    if settings.provider == "fake":
        return OfflineResponder()  # never reads the environment
    raise ProviderUnavailable(
        f"provider {settings.provider!r} is not available in this build yet; use provider = \"fake\""
    )
