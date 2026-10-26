"""A run's model with the ones it switches to: when the user's pick stops answering (it can't be reached, answers with
an error, is rate limited or refuses the key), the next of their models takes over, and the run says so."""

import logging
from collections import Counter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.clients.model import AiKeyInvalidError, ModelClient, ModelRateLimitedError, ModelUnavailableError
from app.constants.ai_keys import KEY_REFUSALS, MAX_MODEL_FALLBACKS
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
    client: ModelClient
    # The user's key it's called with (None: makable's own model). Refusals are counted per key.
    key_id: UUID | None = None


class FallbackModel(ModelClient, Protocol):
    @property
    def switches(self) -> list[ModelSwitch]: ...

    @property
    def refused_keys(self) -> list[UUID]: ...


class FallbackModelClient:
    """Answers with the first candidate (the user's pick) and switches to the next when it fails, at most
    `max_switches` times. A run keeps the model that answered last and never goes back to one that failed. A key that
    refused `KEY_REFUSALS` of its models is skipped (it's the key, not the model); the keys refused and never answered
    with are `refused_keys`, for the service to mark invalid. When no candidate is left, the pick's error is raised."""

    def __init__(
        self,
        candidates: Sequence[ModelCandidate],
        *,
        max_switches: int = MAX_MODEL_FALLBACKS,
        on_switch: Callable[[ModelSwitch], None] | None = None,
    ) -> None:
        if not candidates:
            raise ValueError("a model needs at least one candidate")
        self._candidates = list(candidates)
        self._max_switches = max_switches
        self._on_switch = on_switch
        self._index = 0
        self._switches: list[ModelSwitch] = []
        self._refusals: Counter[UUID | None] = Counter()
        self._answered: set[UUID | None] = set()

    @property
    def name(self) -> str:
        return self._candidates[self._index].client.name

    @property
    def accepts_images(self) -> bool:
        return self._candidates[self._index].client.accepts_images

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
            AiKeyInvalidError, ModelRateLimitedError, ModelUnavailableError: The model, and every one switched to,
                failed. The first one's error is raised.
        """
        first: AppError | None = None
        while True:
            candidate = self._candidates[self._index]
            model = candidate.client
            try:
                reply = await model.complete(messages if model.accepts_images else _without_images(messages))
            except FALLBACK_ERRORS as err:
                failed: AppError = err
            else:
                self._answered.add(candidate.key_id)
                return reply
            first = first or failed
            if isinstance(failed, AiKeyInvalidError):
                self._refusals[candidate.key_id] += 1
            following = self._following()
            if following is None:
                raise first
            self._index = following
            switch = ModelSwitch(failed=model.name, reason=failed.code, model=self.name)
            self._switches.append(switch)
            logger.warning(
                "AI model failed, switching to the next",
                extra={"model": switch.failed, "code": switch.reason, "next": switch.model},
            )
            if self._on_switch is not None:
                self._on_switch(switch)

    def _following(self) -> int | None:
        """The next candidate to switch to, if switches are left: one whose key hasn't been refused too often."""
        if len(self._switches) >= self._max_switches:
            return None
        for index in range(self._index + 1, len(self._candidates)):
            if self._refusals[self._candidates[index].key_id] < KEY_REFUSALS:
                return index
        return None


def _without_images(messages: list[ChatMessage]) -> list[ChatMessage]:
    return [m if isinstance(m.content, str) else ChatMessage(role=m.role, content=m.text) for m in messages]
