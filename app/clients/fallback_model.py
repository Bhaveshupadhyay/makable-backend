"""A run's model with the ones it switches to: when the user's pick stops answering (it can't be reached, answers with
an error, is rate limited or refuses the key), the next of their models takes over, and the run says so."""

import logging
import time
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.clients.model import AiKeyInvalidError, ModelClient, ModelRateLimitedError, ModelUnavailableError
from app.constants.ai_keys import KEY_REFUSALS, MAX_FALLBACK_SECONDS, MAX_MODEL_FALLBACKS
from app.core.exceptions import AppError
from app.schemas.chat import ChatMessage

logger = logging.getLogger(__name__)

# What a model does that switches a run to the next one. Anything else (makable is busy, a bug) isn't the model's.
FALLBACK_ERRORS = (ModelUnavailableError, ModelRateLimitedError, AiKeyInvalidError)

_REASONS = {
    ModelRateLimitedError.code: "is rate limiting your key",
    AiKeyInvalidError.code: "didn't accept your key",
}


@dataclass(frozen=True)
class ModelSwitch:
    """A run's model failed (`reason` is its error's code) and `model` took over."""

    failed: str
    reason: str
    model: str

    @property
    def message(self) -> str:
        """What the user is told."""
        reason = _REASONS.get(self.reason, "isn't answering")
        return f"{self.failed} {reason}, so {self.model} is taking over."


@dataclass(frozen=True)
class ModelCandidate:
    """A model a run can switch to. It's built when the run gets to it, so a fallback's key is only decrypted if the
    run needs it."""

    connect: Callable[[], ModelClient]
    # The user's key it's called with. Refusals are counted per key.
    key_id: UUID


class FallbackModel(ModelClient, Protocol):
    @property
    def switches(self) -> list[ModelSwitch]: ...

    @property
    def refused_keys(self) -> list[UUID]: ...


class FallbackModelClient:
    """Answers with `model` (the user's pick, on `key_id`; None: makable's own) and switches to the next of
    `fallbacks` when it fails, at most `max_switches` times, and not once the run's failed calls took
    `max_failed_seconds`. A run keeps the model that answered last and never goes back to one that failed. A key that
    refused `KEY_REFUSALS` of its models is skipped (it's the key, not the model), and so is a fallback that can't be
    built; the keys refused and never answered with are `refused_keys`, for the service to mark invalid. When no
    switch is left, the first error of the call is raised (the pick's, unless the run had switched before)."""

    def __init__(
        self,
        model: ModelClient,
        *,
        key_id: UUID | None = None,
        fallbacks: Sequence[ModelCandidate] = (),
        max_switches: int = MAX_MODEL_FALLBACKS,
        max_failed_seconds: float = MAX_FALLBACK_SECONDS,
        on_switch: Callable[[ModelSwitch], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._model = model
        self._key_id = key_id
        self._fallbacks = list(fallbacks)
        self._max_switches = max_switches
        self._max_failed_seconds = max_failed_seconds
        self._on_switch = on_switch
        self._clock = clock
        # The first fallback not tried yet.
        self._next = 0
        self._failed_seconds = 0.0
        self._switches: list[ModelSwitch] = []
        self._refusals: Counter[UUID | None] = Counter()
        self._answered: set[UUID | None] = set()

    @property
    def name(self) -> str:
        return self._model.name

    @property
    def accepts_images(self) -> bool:
        return self._model.accepts_images

    @property
    def switches(self) -> list[ModelSwitch]:
        return list(self._switches)

    @property
    def refused_keys(self) -> list[UUID]:
        return [key for key in self._refusals if key is not None and key not in self._answered]

    async def complete(self, messages: list[ChatMessage]) -> str:
        """The reply of the current model, or of the next ones if it fails. A model that doesn't read images gets the
        messages without them.

        Raises:
            AiKeyInvalidError, ModelRateLimitedError, ModelUnavailableError: The model failed, and so did every one it
                switched to (or no switch was left).
        """
        first: AppError | None = None
        while True:
            model, key_id = self._model, self._key_id
            started = self._clock()
            try:
                reply = await model.complete(messages if model.accepts_images else _without_images(messages))
            except FALLBACK_ERRORS as err:
                failed: AppError = err
            else:
                self._answered.add(key_id)
                return reply
            self._failed_seconds += self._clock() - started
            first = first or failed
            if isinstance(failed, AiKeyInvalidError):
                self._refusals[key_id] += 1
            if not self._switch_from(model, failed):
                raise first

    def _switch_from(self, failed: ModelClient, error: AppError) -> bool:
        """Moves on to the next fallback, if a switch is left: one whose key hasn't refused too often, and that can
        be built."""
        if len(self._switches) >= self._max_switches or self._failed_seconds >= self._max_failed_seconds:
            return False
        while self._next < len(self._fallbacks):
            candidate = self._fallbacks[self._next]
            self._next += 1
            if self._refusals[candidate.key_id] >= KEY_REFUSALS:
                continue
            try:
                model = candidate.connect()
            except Exception:
                logger.exception("Couldn't set up a fallback AI model")
                continue
            self._model, self._key_id = model, candidate.key_id
            switch = ModelSwitch(failed=failed.name, reason=error.code, model=model.name)
            self._switches.append(switch)
            logger.warning(
                "AI model failed, switching to the next",
                extra={"model": switch.failed, "code": switch.reason, "next": switch.model},
            )
            if self._on_switch is not None:
                self._on_switch(switch)
            return True
        return False


def _without_images(messages: list[ChatMessage]) -> list[ChatMessage]:
    return [m if isinstance(m.content, str) else ChatMessage(role=m.role, content=m.text) for m in messages]
