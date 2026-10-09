"""The real model client against a mocked HTTP transport."""

import json
from collections.abc import Callable

import httpx
import pytest

from app.clients.model import HttpModelClient, ModelUnavailableError
from app.schemas.chat import ChatMessage

URL = "http://model.test/v1"
MESSAGES = [ChatMessage(role="user", content="hi")]


def client(handler: Callable[[httpx.Request], httpx.Response], api_key: str | None = "k") -> HttpModelClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpModelClient(http, base_url=URL, api_key=api_key, model="auto", timeout_seconds=5)


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
