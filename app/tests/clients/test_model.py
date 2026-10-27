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


def users_model(handler: Callable[[httpx.Request], httpx.Response]) -> HttpModelClient:
    """A model on a user's own key: its provider is named in its errors."""
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    endpoint = ModelEndpoint(base_url=URL, api_key="k", model="gemini-3.5-flash", provider="Google Gemini")
    return HttpModelClient(http, endpoint, timeout_seconds=90)


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (
            httpx.Response(400, json=[{"error": {"message": "This model only supports Interactions API."}}]),
            "Your AI provider (Google Gemini) returned an error for gemini-3.5-flash: This model only supports "
            "Interactions API.",
        ),
        (
            httpx.Response(402, json={"error": {"message": "This request requires\n  more credits."}}),
            "Your AI provider (Google Gemini) returned an error for gemini-3.5-flash: This request requires more "
            "credits.",
        ),
        (
            httpx.Response(503, text="<html>down</html>"),
            "Your AI provider (Google Gemini) returned an error for gemini-3.5-flash (HTTP 503).",
        ),
        (
            httpx.Response(200, json={"choices": [{"message": {"content": None}}]}),
            "Your AI provider (Google Gemini) sent an empty reply from gemini-3.5-flash.",
        ),
    ],
)
async def test_a_users_provider_error_is_told_in_its_own_words(response: httpx.Response, message: str) -> None:
    with pytest.raises(ModelUnavailableError) as err:
        await users_model(lambda _: response).complete(MESSAGES)
    assert err.value.message == message


async def test_a_long_provider_message_is_cut() -> None:
    response = httpx.Response(500, json={"error": {"message": "x" * 1000}})
    with pytest.raises(ModelUnavailableError) as err:
        await users_model(lambda _: response).complete(MESSAGES)
    assert err.value.message.endswith("x…")
    assert len(err.value.message) < 500


async def test_a_users_provider_out_of_quota_says_so_and_keeps_the_wait() -> None:
    said = "You have no credits remaining. Add credits to continue using the API."
    response = httpx.Response(429, headers={"retry-after": "12"}, json={"error": {"message": said}})
    with pytest.raises(ModelRateLimitedError) as err:
        await users_model(lambda _: response).complete(MESSAGES)
    assert err.value.message == f"Your AI provider (Google Gemini) returned an error for gemini-3.5-flash: {said}"
    assert err.value.details == {"retryAfter": 12}


@pytest.mark.parametrize(
    ("raised", "message"),
    [
        (
            httpx.ReadTimeout(""),
            "gemini-3.5-flash at your AI provider (Google Gemini) didn't answer within 90 seconds. Try again, or pick "
            "another model.",
        ),
        (
            httpx.ConnectError("refused"),
            "makable couldn't reach your AI provider (Google Gemini). Try again in a moment.",
        ),
    ],
)
async def test_a_users_provider_that_doesnt_answer_is_named(raised: httpx.HTTPError, message: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise raised

    with pytest.raises(ModelUnavailableError) as err:
        await users_model(handler).complete(MESSAGES)
    assert err.value.message == message


async def test_makables_own_model_keeps_the_general_messages() -> None:
    error = httpx.Response(400, json={"error": {"message": "OmniRoute: no route for auto"}})
    with pytest.raises(ModelUnavailableError) as err:
        await client(lambda _: error).complete(MESSAGES)
    assert err.value.message == ModelUnavailableError.message

    limited = httpx.Response(429, json={"error": {"message": "slow down"}})
    with pytest.raises(ModelRateLimitedError) as rate_limited:
        await client(lambda _: limited).complete(MESSAGES)
    assert rate_limited.value.message == ModelRateLimitedError.message


@pytest.mark.parametrize("status_code", [400, 429])
async def test_credentials_a_provider_echoes_are_taken_out_of_its_message(status_code: int) -> None:
    # The request's own key, whatever its shape (Mistral's have no prefix), and anything shaped like a key.
    key = "Zq7mN2pL9xR4tY6w"
    said = (
        f"Key {key} can't use this model. Also seen: sk-proj-AbC123dEf456, AIzaSyD1234567890abcdefghij, "
        "Bearer eyJhbGciOi.x.y, https://example.com/v1?key=secret123&alt=json"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": {"message": said}})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    endpoint = ModelEndpoint(base_url=URL, api_key=key, model="m", provider="Mistral")
    with pytest.raises((ModelUnavailableError, ModelRateLimitedError)) as err:
        await HttpModelClient(http, endpoint, timeout_seconds=5).complete(MESSAGES)

    assert err.value.message == (
        "Your AI provider (Mistral) returned an error for m: Key [redacted] can't use this model. Also seen: "
        "[redacted], [redacted], [redacted], https://example.com/v1?key=[redacted]&alt=json"
    )
