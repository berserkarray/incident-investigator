"""Keeps API key values out of traces, reports, logs and error messages (Req 5.10, 31.11)."""

import logging
import os
from collections.abc import Iterable, Mapping
from typing import Any

API_KEY_ENV_VARS: tuple[str, ...] = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")
REDACTED = "[REDACTED]"


class SecretRedactor:
    """Replaces every occurrence of a known secret value with ``[REDACTED]``."""

    def __init__(self, secrets: Iterable[str] = ()):
        # Longest first so a secret that contains another is replaced whole.
        self._secrets = tuple(sorted({s for s in secrets if s}, key=len, reverse=True))

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "SecretRedactor":
        env = os.environ if env is None else env
        return cls(env.get(name, "") for name in API_KEY_ENV_VARS)

    @property
    def active(self) -> bool:
        return bool(self._secrets)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
        return text

    def redact_obj(self, value: Any) -> Any:
        """Redact every string inside nested dicts, lists and tuples."""
        if not self._secrets:
            return value
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, Mapping):
            return {self.redact_obj(k): self.redact_obj(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self.redact_obj(v) for v in value)
        return value


class RedactingFilter(logging.Filter):
    """Logging filter that redacts the formatted message of every record."""

    def __init__(self, redactor: SecretRedactor):
        super().__init__()
        self._redactor = redactor

    def filter(self, record: logging.LogRecord) -> bool:
        if self._redactor.active:
            record.msg = self._redactor.redact(record.getMessage())
            record.args = ()
        return True
