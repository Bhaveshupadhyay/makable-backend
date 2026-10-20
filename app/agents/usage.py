"""What a run takes: its model calls, the tokens the provider counted for them, and the time.

A run's usage is counted in its context (`UsageMeter`), so a run that fails or is stopped still has it, and saved in
its state (`usage`, set by every node that calls the model) so it carries over a pause: a run that asked the user
something continues, in a later request, from what it took before. The time is the run's own (its requests'), not
the time it waited for the user or the preview.
"""

import time
from typing import TypedDict

from app.clients.model import ModelClient, ModelReply
from app.schemas.chat import ChatMessage


class Usage(TypedDict):
    """A run's usage so far. A token count is None when the provider didn't say: `input_tokens`/`output_tokens` when
    any reply came without them (the sum would be short), the others when no reply had them."""

    model_calls: int
    input_tokens: int | None
    output_tokens: int | None
    reasoning_tokens: int | None
    cached_tokens: int | None
    cost_usd: float | None
    # Waiting for the model, and the whole run.
    model_seconds: float
    seconds: float


def no_usage() -> Usage:
    return Usage(
        model_calls=0,
        input_tokens=0,
        output_tokens=0,
        reasoning_tokens=None,
        cached_tokens=None,
        cost_usd=None,
        model_seconds=0.0,
        seconds=0.0,
    )


def shown_usage(usage: Usage | None, show: bool) -> Usage | None:
    """The usage to show the user: only when the service says so (the run used the user's own key) and the model was
    called."""
    return usage if show and usage is not None and usage["model_calls"] else None


class UsageMeter:
    """Counts a run's usage in this request, on top of what it took before (in earlier requests, before a pause)."""

    def __init__(self, before: Usage | None = None) -> None:
        self._usage = before or no_usage()
        self._started = time.perf_counter()

    def record(self, reply: ModelReply) -> None:
        tokens = reply.usage
        usage = self._usage
        self._usage = Usage(
            model_calls=usage["model_calls"] + 1,
            input_tokens=_all(usage["input_tokens"], tokens.input if tokens else None),
            output_tokens=_all(usage["output_tokens"], tokens.output if tokens else None),
            reasoning_tokens=_any(usage["reasoning_tokens"], tokens.reasoning if tokens else None),
            cached_tokens=_any(usage["cached_tokens"], tokens.cached if tokens else None),
            cost_usd=_any(usage["cost_usd"], tokens.cost_usd if tokens else None),
            model_seconds=round(usage["model_seconds"] + reply.seconds, 3),
            seconds=usage["seconds"],
        )

    def total(self) -> Usage:
        """The run's usage so far, its time up to now."""
        return {**self._usage, "seconds": round(self._usage["seconds"] + time.perf_counter() - self._started, 3)}


class MeteredModel:
    """A model whose replies are counted in a run's meter."""

    def __init__(self, model: ModelClient, meter: UsageMeter) -> None:
        self._model = model
        self._meter = meter

    @property
    def name(self) -> str:
        return self._model.name

    @property
    def accepts_images(self) -> bool:
        return self._model.accepts_images

    async def complete(self, messages: list[ChatMessage]) -> ModelReply:
        reply = await self._model.complete(messages)
        self._meter.record(reply)
        return reply


def _all[T: (int, float)](total: T | None, value: T | None) -> T | None:
    return None if total is None or value is None else total + value


def _any[T: (int, float)](total: T | None, value: T | None) -> T | None:
    return value if total is None else total if value is None else total + value
