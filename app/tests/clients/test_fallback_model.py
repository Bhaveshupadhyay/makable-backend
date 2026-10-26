"""The model a run uses: the user's pick, and the models it switches to when the one answering fails."""

from collections.abc import Callable
from uuid import UUID

import pytest

from app.clients.fallback_model import FallbackModelClient, ModelCandidate, ModelSwitch
from app.clients.model import AiKeyInvalidError, ModelRateLimitedError, ModelUnavailableError
from app.core.limits import ServiceBusyError
from app.schemas.chat import ChatMessage, ImagePart, ImageUrl, TextPart
from app.tests.fakes import FakeModelClient

MESSAGES = [ChatMessage(role="user", content="hi")]
KEY = UUID(int=1)
OTHER_KEY = UUID(int=2)


def fallback(
    *models: FakeModelClient,
    keys: list[UUID] | None = None,
    max_switches: int = 5,
    on_switch: Callable[[ModelSwitch], None] | None = None,
) -> FallbackModelClient:
    """The models as candidates, on `keys` (all on `KEY` if not given)."""
    candidates = [ModelCandidate(model, key) for model, key in zip(models, keys or [KEY] * len(models), strict=True)]
    return FallbackModelClient(candidates, max_switches=max_switches, on_switch=on_switch)


async def test_the_pick_answers_when_it_works() -> None:
    pick, other = FakeModelClient("hello", name="pick"), FakeModelClient(name="other")
    model = fallback(pick, other)

    assert await model.complete(MESSAGES) == "hello"
    assert (model.name, model.switches, other.calls) == ("pick", [], [])


@pytest.mark.parametrize(
    "error", [ModelUnavailableError(), ModelRateLimitedError(30), AiKeyInvalidError()], ids=lambda e: e.code
)
async def test_a_failing_model_switches_to_the_next_and_the_run_keeps_it(error: Exception) -> None:
    pick = FakeModelClient(error, name="gemini-antigravity")
    other = FakeModelClient("first", "second", name="gemini-2.5-flash")
    heard: list[ModelSwitch] = []
    model = fallback(pick, other, on_switch=heard.append)

    assert await model.complete(MESSAGES) == "first"
    # The next call goes to the model that answered: the failed one isn't tried again.
    assert await model.complete(MESSAGES) == "second"
    assert len(pick.calls) == 1
    assert model.name == "gemini-2.5-flash"
    switch = ModelSwitch(failed="gemini-antigravity", reason=getattr(error, "code", ""), model="gemini-2.5-flash")
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


def test_a_model_needs_a_candidate() -> None:
    with pytest.raises(ValueError, match="candidate"):
        FallbackModelClient([])
