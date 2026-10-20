"""RetryingLLMClient: LLM retries and timeouts on the Injectable_Clock (Req 5.7, 5.8).

Provider SDK clients are built with ``max_retries=0``, so this wrapper is the only place an LLM call
is retried. Rate-limit, server and timeout errors are retried until ``llm.max_attempts`` attempts in
total; the wait before retry ``k`` (k = 1, 2, ...) is ``min(max_delay_s, base_delay_s * 2**(k - 1))``.
Every attempt runs under ``clock.wait_for(..., request_timeout_s)``, so tests never wait on the real
clock. Any other error category ends the call at once.
"""

from ..clock import Clock, ClockTimeout
from ..models.config import LLMSettings
from .base import RETRYABLE_CATEGORIES, LLMCallError, LLMClient, LLMErrorCategory, LLMRequest, LLMResponse


def retry_delay(settings: LLMSettings, retry_no: int) -> float:
    """Seconds to wait before retry ``retry_no`` (1-based)."""
    return min(settings.max_delay_s, settings.base_delay_s * 2 ** (retry_no - 1))


class RetryingLLMClient:
    def __init__(self, inner: LLMClient, clock: Clock, settings: LLMSettings):
        self.inner = inner
        self._clock = clock
        self._settings = settings

    async def complete(self, request: LLMRequest) -> LLMResponse:
        attempts = self._settings.max_attempts
        timeout = self._settings.request_timeout_s
        retried: list[str] = []
        for attempt in range(1, attempts + 1):
            try:
                response = await self._clock.wait_for(self.inner.complete(request), timeout)
                if not retried:
                    return response
                # Keep the retries visible in the Run_Trace (the gateway records attempts per call).
                return response.model_copy(update={"attempts": attempt, "retried_errors": tuple(retried)})
            except ClockTimeout:
                error = LLMCallError(LLMErrorCategory.TIMEOUT, f"no response within {timeout:g} s", attempts=attempt)
            except LLMCallError as exc:
                error = LLMCallError(exc.category, _detail(exc), attempts=attempt)
            if error.category not in RETRYABLE_CATEGORIES or attempt == attempts:
                raise error
            retried.append(str(error.category))
            await self._clock.sleep(retry_delay(self._settings, attempt))
        raise AssertionError("unreachable: the last attempt either returns or raises")


def _detail(exc: LLMCallError) -> str:
    """The message of an LLMCallError without its ``"<category>: "`` prefix."""
    text = str(exc)
    prefix = f"{exc.category}: "
    return text[len(prefix) :] if text.startswith(prefix) else text
