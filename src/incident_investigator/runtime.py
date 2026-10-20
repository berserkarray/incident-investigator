"""Injected dependencies of one Run, shared by the Investigator and the Baseline_Agent."""

from dataclasses import dataclass, field

from .clock import Clock, SystemClock
from .llm.base import LLMClient
from .redact import SecretRedactor


@dataclass(frozen=True)
class RunDependencies:
    llm_client: LLMClient
    clock: Clock = field(default_factory=SystemClock)
    redactor: SecretRedactor = field(default_factory=SecretRedactor)
