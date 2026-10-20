"""ScriptedFakeLLM: per-test scripted responses with synthetic usage (Req 5.11, 5.12, 30.4)."""

from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from ..clock import Clock
from ..models.budget import TokenCount
from ..models.vocab import Caller, CallKind
from .base import LLMCallError, LLMErrorCategory, LLMRequest, LLMResponse
from .context import estimate_tokens


class FakeScriptExhausted(Exception):
    def __init__(self, caller: Caller, call_kind: CallKind):
        super().__init__(f"Fake_LLM script has no response for {caller} ({call_kind})")
        self.caller = caller
        self.call_kind = call_kind


@dataclass(frozen=True)
class ScriptEntry:
    """One scripted response. Give ``text`` or ``respond``; ``call_kind=None`` matches any kind."""

    caller: Caller
    call_kind: CallKind | None = None
    text: str | None = None
    respond: Callable[[LLMRequest], str] | None = None
    usage: TokenCount | None = None
    delay_s: float = 0.0
    raise_category: LLMErrorCategory | None = None


class RecordingMixin:
    def __init__(self) -> None:
        self.prompts: list[LLMRequest] = []

    @staticmethod
    def synthetic_usage(request: LLMRequest, text: str) -> TokenCount:
        return TokenCount(
            input_tokens=estimate_tokens(request.system + request.user), output_tokens=estimate_tokens(text)
        )


class ScriptedFakeLLM(RecordingMixin):
    """Entries are consumed FIFO per ``(caller, call_kind)``, falling back to entries for the caller with
    ``call_kind=None``. A missing entry raises ``FakeScriptExhausted`` naming the caller."""

    def __init__(self, entries: Sequence[ScriptEntry], clock: Clock):
        super().__init__()
        self._clock = clock
        self._queues: dict[tuple[str, str | None], deque[ScriptEntry]] = {}
        for entry in entries:
            key = (str(entry.caller), str(entry.call_kind) if entry.call_kind else None)
            self._queues.setdefault(key, deque()).append(entry)

    def remaining(self) -> int:
        return sum(len(q) for q in self._queues.values())

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.prompts.append(request)
        queue = self._queues.get((str(request.caller), str(request.call_kind)))
        if not queue:
            queue = self._queues.get((str(request.caller), None))
        if not queue:
            raise FakeScriptExhausted(request.caller, request.call_kind)
        entry = queue.popleft()
        if entry.delay_s:
            await self._clock.sleep(entry.delay_s)
        if entry.raise_category is not None:
            raise LLMCallError(entry.raise_category, "scripted provider error")
        text = entry.respond(request) if entry.respond else (entry.text or "")
        usage = entry.usage or self.synthetic_usage(request, text)
        return LLMResponse(text=text, usage=usage, provider="fake")
