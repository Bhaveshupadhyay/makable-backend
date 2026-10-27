"""The model a run uses: the user's pick, and the models it switches to when the one answering fails."""

import time
from collections.abc import Callable
from uuid import UUID

import pytest

from app.clients.fallback_model import FallbackModelClient, ModelCandidate, ModelSwitch
from app.clients.model import AiKeyInvalidError, ModelRateLimitedError, ModelUnavailableError
from app.core.limits import ServiceBusyError
from app.schemas.chat import ChatMessage, ImagePart, ImageUrl, TextPart
from app.tests.fakes import FakeModelClient, connect_to

MESSAGES = [ChatMessage(role="user", content="hi")]
KEY = UUID(int=1)
OTHER_KEY = UUID(int=2)


def fallback(
    pick: FakeModelClient,
    *fallbacks: FakeModelClient,
    keys: list[UUID] | None = None,
    max_switches: int = 5,
    max_failed_seconds: float = 100,
    on_switch: Callable[[ModelSwitch], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> FallbackModelClient:
    """The pick and its fallbacks, on `keys` (all on `KEY` if not given)."""
    pick_key, *fallback_keys = keys or [KEY] * (1 + len(fallbacks))
    candidates = [ModelCandidate(connect_to(m), key) for m, key in zip(fallbacks, fallback_keys, strict=True)]
    return FallbackModelClient(
        pick,
        key_id=pick_key,
        fallbacks=candidates,
        max_switches=max_switches,
        max_failed_seconds=max_failed_seconds,
        on_switch=on_switch,
        clock=clock,
    )


class SlowFailure(FakeModelClient):
    """Fails with `ModelUnavailableError` after `seconds` on `clock` (a model that times out)."""

    def __init__(self, clock: list[float], seconds: float, name: str) -> None:
        super().__init__(name=name)
        self.clock = clock
        self.seconds = seconds

    async def complete(self, messages: list[ChatMessage]) -> str:
        self.calls.append(list(messages))
        self.clock[0] += self.seconds
        raise ModelUnavailableError()


async def test_the_pick_answers_when_it_works() -> None:
    pick, other = FakeModelClient("hello", name="pick"), FakeModelClient(name="other")
    model = fallback(pick, other)

    assert await model.complete(MESSAGES) == "hello"
    assert (model.name, model.switches, other.calls) == ("pick", [], [])


@pytest.mark.parametrize(
    "error", [ModelUnavailableError(), ModelRateLimitedError(30), AiKeyInvalidError()], ids=lambda e: e.code
)
async def test_a_failing_model_switches_to_the_next_and_the_run_keeps_it(error: Exception) -> None:
    pick = FakeModelClient(error, name="gemini-3.8-flash")
    other = FakeModelClient("first", "second", name="gemini-2.5-flash")
    heard: list[ModelSwitch] = []
    model = fallback(pick, other, on_switch=heard.append)

    assert await model.complete(MESSAGES) == "first"
    # The next call goes to the model that answered: the failed one isn't tried again.
    assert await model.complete(MESSAGES) == "second"
    assert len(pick.calls) == 1
    assert model.name == "gemini-2.5-flash"
    switch = ModelSwitch(failed="gemini-3.8-flash", reason=getattr(error, "code", ""), model="gemini-2.5-flash")
    assert model.switches == heard == [switch]


def test_the_switch_says_what_happened() -> None:
    def said(reason: str) -> str:
        return ModelSwitch(failed="a", reason=reason, model="b").message

    assert said("ai_unavailable") == "a isn't answering, so b is taking over."
    assert said("ai_rate_limited") == "a is rate limiting your key, so b is taking over."
    assert said("ai_key_invalid") == "a didn't accept your key, so b is taking over."


async def test_it_switches_at_most_max_switches_times_and_raises_the_picks_error() -> None:
    picked = ModelRateLimitedError(30)
    failing = [FakeModelClient(picked, name="m0")]
    failing += [FakeModelClient(ModelUnavailableError(), name=f"m{i}") for i in range(1, 7)]
    model = fallback(*failing, max_switches=5)

    with pytest.raises(ModelRateLimitedError) as err:
        await model.complete(MESSAGES)

    assert err.value is picked
    assert [len(m.calls) for m in failing] == [1, 1, 1, 1, 1, 1, 0]
    assert len(model.switches) == 5


async def test_errors_that_arent_the_models_dont_switch() -> None:
    other = FakeModelClient("hi", name="other")
    model = fallback(FakeModelClient(ServiceBusyError()), other)

    with pytest.raises(ServiceBusyError):
        await model.complete(MESSAGES)
    assert (other.calls, model.switches) == ([], [])


async def test_a_key_that_answers_with_another_model_is_not_refused() -> None:
    # A provider can refuse one model (a preview the key has no access to) and take the key's others.
    model = fallback(FakeModelClient(AiKeyInvalidError()), FakeModelClient("hi"))

    assert await model.complete(MESSAGES) == "hi"
    assert model.refused_keys == []


async def test_a_key_refused_twice_is_skipped_and_reported() -> None:
    first, second = FakeModelClient(AiKeyInvalidError()), FakeModelClient(AiKeyInvalidError())
    third, other = FakeModelClient("never"), FakeModelClient("hi", name="other")
    model = fallback(first, second, third, other, keys=[KEY, KEY, KEY, OTHER_KEY])

    assert await model.complete(MESSAGES) == "hi"
    assert third.calls == []
    assert model.refused_keys == [KEY]
    assert [s.model for s in model.switches] == ["fake-model", "other"]


async def test_a_refused_key_left_behind_is_reported() -> None:
    model = fallback(FakeModelClient(AiKeyInvalidError()), FakeModelClient("hi"), keys=[KEY, OTHER_KEY])

    await model.complete(MESSAGES)

    assert model.refused_keys == [KEY]


async def test_a_model_that_reads_text_only_gets_the_messages_without_their_pictures() -> None:
    picture = ImagePart(image_url=ImageUrl(url="data:image/png;base64,AAAA"))
    messages = [ChatMessage(role="user", content=[TextPart(text="make it blue"), picture])]
    pick = FakeModelClient(ModelUnavailableError(), accepts_images=True)
    text_only = FakeModelClient("ok")

    await fallback(pick, text_only).complete(messages)

    assert pick.calls == [messages]
    assert text_only.calls == [[ChatMessage(role="user", content="make it blue")]]


async def test_it_stops_switching_once_failed_calls_took_too_long() -> None:
    # Two models that time out: the first hands over, the second (past the budget) gives up.
    now = [0.0]
    first, second = SlowFailure(now, 90, "first"), SlowFailure(now, 90, "second")
    third = FakeModelClient("never", name="third")
    model = fallback(first, second, third, max_failed_seconds=100, clock=lambda: now[0])

    with pytest.raises(ModelUnavailableError):
        await model.complete(MESSAGES)

    assert (len(first.calls), len(second.calls), third.calls) == (1, 1, [])
    assert [s.model for s in model.switches] == ["second"]


async def test_fast_failures_dont_use_up_the_time() -> None:
    now = [0.0]
    failing = [SlowFailure(now, 1, f"m{i}") for i in range(3)]
    model = fallback(*failing, FakeModelClient("hi"), max_failed_seconds=100, clock=lambda: now[0])

    assert await model.complete(MESSAGES) == "hi"
    assert len(model.switches) == 3


async def test_fallbacks_are_built_only_when_switched_to_and_one_that_cant_be_is_skipped() -> None:
    built: list[str] = []

    def broken() -> FakeModelClient:
        built.append("broken")
        raise ValueError("the key can't be decrypted")

    def working() -> FakeModelClient:
        built.append("working")
        return FakeModelClient("hi", name="working")

    pick = FakeModelClient("first", ModelUnavailableError(), name="pick")
    model = FallbackModelClient(
        pick, key_id=KEY, fallbacks=[ModelCandidate(broken, OTHER_KEY), ModelCandidate(working, OTHER_KEY)]
    )

    assert await model.complete(MESSAGES) == "first"
    assert built == []
    assert await model.complete(MESSAGES) == "hi"
    assert built == ["broken", "working"]
    assert [s.model for s in model.switches] == ["working"]
