"""A run's usage: what its model calls took, added up across replies and pauses."""

from app.agents.usage import MeteredModel, Usage, UsageMeter, no_usage, shown_usage
from app.clients.model import ModelReply, TokenUsage
from app.schemas.chat import ChatMessage
from app.tests.fakes import FakeModelClient

MESSAGES = [ChatMessage(role="user", content="hi")]


def test_replies_are_added_up() -> None:
    meter = UsageMeter()

    meter.record(ModelReply("a", TokenUsage(100, 20, reasoning=5, cached=60), seconds=1.5))
    meter.record(ModelReply("b", TokenUsage(150, 30, cached=100, cost_usd=0.01), seconds=2.25))

    usage = meter.total()
    assert {k: v for k, v in usage.items() if k != "seconds"} == {
        "model_calls": 2,
        "input_tokens": 250,
        "output_tokens": 50,
        "reasoning_tokens": 5,
        "cached_tokens": 160,
        "cost_usd": 0.01,
        "model_seconds": 3.75,
    }
    assert usage["seconds"] >= 0


def test_a_reply_without_usage_leaves_the_counts_unknown() -> None:
    meter = UsageMeter()

    meter.record(ModelReply("a", TokenUsage(100, 20, reasoning=5)))
    meter.record(ModelReply("b"))

    usage = meter.total()
    assert (usage["model_calls"], usage["input_tokens"], usage["output_tokens"]) == (2, None, None)
    # What one reply said is kept.
    assert usage["reasoning_tokens"] == 5


def test_a_run_goes_on_from_what_it_took_before_its_pause() -> None:
    before = Usage(**{**no_usage(), "model_calls": 1, "input_tokens": 10, "output_tokens": 2, "seconds": 4.0})
    meter = UsageMeter(before)

    meter.record(ModelReply("b", TokenUsage(30, 3)))

    usage = meter.total()
    assert (usage["model_calls"], usage["input_tokens"], usage["output_tokens"]) == (2, 40, 5)
    assert 4.0 <= usage["seconds"] < 5.0


async def test_a_metered_model_counts_its_replies() -> None:
    meter = UsageMeter()
    model = MeteredModel(FakeModelClient(ModelReply("hi", TokenUsage(7, 1))), meter)

    reply = await model.complete(MESSAGES)

    assert reply.text == "hi"
    assert (model.name, model.accepts_images) == ("fake-model", False)
    assert meter.total()["input_tokens"] == 7


def test_usage_is_shown_only_when_asked_and_the_model_was_called() -> None:
    called = Usage(**{**no_usage(), "model_calls": 1})

    assert shown_usage(called, True) == called
    assert shown_usage(called, False) is None
    assert shown_usage(no_usage(), True) is None
    assert shown_usage(None, True) is None
