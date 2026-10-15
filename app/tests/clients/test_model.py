"""The real model client against a mocked HTTP transport."""

import json
from collections.abc import Callable

import httpx
import pytest

from app.clients.model import AiKeyInvalidError, HttpModelClient, ModelRateLimitedError, ModelUnavailableError
from app.core.limits import ServiceBusyError
from app.schemas.ai_key import ModelEndpoint
from app.schemas.chat import ChatMessage, ImagePart, ImageUrl, TextPart

URL = "http://model.test/v1"
MESSAGES = [ChatMessage(role="user", content="hi")]


def client(handler: Callable[[httpx.Request], httpx.Response], api_key: str | None = "k") -> HttpModelClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpModelClient(http, ModelEndpoint(base_url=URL, api_key=api_key, model="auto"), timeout_seconds=5)


async def test_sends_the_chat_and_returns_the_reply_text() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}]})

    assert await client(handler).complete(MESSAGES) == "hello"
    [request] = seen
    assert str(request.url) == f"{URL}/chat/completions"
    assert request.headers["authorization"] == "Bearer k"
    assert json.loads(request.content) == {
        "model": "auto",
        "messages": [{"role": "user", "content": "hi"}],
        "temperature": 0.2,
        "stream": False,
    }


async def test_sends_no_authorization_without_a_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    assert await client(handler, api_key=None).complete(MESSAGES) == "ok"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(502, json={"error": {"message": "upstream down"}}),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, text="not json"),
        httpx.Response(400, json=[{"error": {"code": 400, "message": "model auto not found"}}]),
    ],
)
async def test_errors_and_empty_replies_are_unavailable(response: httpx.Response) -> None:
    with pytest.raises(ModelUnavailableError):
        await client(lambda _: response).complete(MESSAGES)


async def test_an_unreachable_model_is_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ModelUnavailableError):
        await client(handler).complete(MESSAGES)


async def test_no_free_connection_is_a_quick_busy_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.PoolTimeout("no free connection")

    with pytest.raises(ServiceBusyError):
        await client(handler).complete(MESSAGES)


async def test_sends_the_endpoints_extra_headers() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == "k"
        assert request.headers["authorization"] == "Bearer k"
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    endpoint = ModelEndpoint(base_url=URL, api_key="k", model="m", headers={"x-api-key": "k"})
    assert await HttpModelClient(http, endpoint, timeout_seconds=5).complete(MESSAGES) == "ok"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": {"message": "bad key"}}),
        httpx.Response(403, json={"error": {"message": "bad key"}}),
        # Gemini's answer to a bad key.
        httpx.Response(
            400, json=[{"error": {"code": 400, "message": "API key not valid. Please pass a valid API key."}}]
        ),
    ],
)
async def test_a_refused_key_is_its_own_error(response: httpx.Response) -> None:
    with pytest.raises(AiKeyInvalidError):
        await client(lambda _: response).complete(MESSAGES)


async def test_a_rate_limit_carries_the_providers_wait() -> None:
    response = httpx.Response(429, headers={"retry-after": "12"})
    with pytest.raises(ModelRateLimitedError) as err:
        await client(lambda _: response).complete(MESSAGES)
    assert err.value.details == {"retryAfter": 12}
    assert err.value.headers == {"Retry-After": "12"}


async def test_a_message_with_a_picture_is_sent_as_content_parts() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    endpoint = ModelEndpoint(base_url="https://api.example.com/v1", api_key="k", model="gpt-4o", accepts_images=True)
    model = HttpModelClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)), endpoint, timeout_seconds=5)
    message = ChatMessage(role="user", content=[TextPart(text="hi"), ImagePart(image_url=ImageUrl(url="data:x"))])

    assert await model.complete([message]) == "ok"
    assert model.accepts_images
    assert seen[0]["messages"] == [
        {
            "role": "user",
            "content": [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": "data:x"}}],
        }
    ]
