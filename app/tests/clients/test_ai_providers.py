"""The real provider client against a mocked HTTP transport."""

from collections.abc import Callable

import httpx
import pytest

from app.clients.ai_providers import AiProviderError, HttpAiProviderClient, model_accepts_images, provider_endpoint
from app.clients.model import AiKeyInvalidError, ModelRateLimitedError
from app.constants.ai_keys import AiProvider


def client(handler: Callable[[httpx.Request], httpx.Response]) -> HttpAiProviderClient:
    return HttpAiProviderClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def models(*ids: str) -> httpx.Response:
    return httpx.Response(200, json={"object": "list", "data": [{"id": i} for i in ids]})


@pytest.mark.parametrize(
    "model",
    [
        "gpt-4o",
        "gpt-5-mini",
        "o1",
        "o3",
        "o4-mini",
        "claude-sonnet-4-5",
        "claude-3-haiku-20240307",
        "models/gemini-2.5-flash",
        "gemma-3-27b-it",
        "meta-llama/llama-4-scout-17b-16e-instruct",
        "qwen2.5-vl-72b",
    ],
)
def test_models_that_read_images_get_the_picture(model: str) -> None:
    assert model_accepts_images(model)


@pytest.mark.parametrize(
    "model",
    [
        "o1-mini",
        "o1-preview",
        "o3-mini",
        "openai/o3-mini",
        "gpt-4-turbo-preview",
        "gpt-4o-search-preview",
        "claude-3-5-haiku-20241022",
        "gemma-3-1b-it",
        "deepseek-chat",
        "llama-3.3-70b-versatile",
    ],
)
def test_text_only_models_get_no_picture(model: str) -> None:
    assert not model_accepts_images(model)


async def test_lists_chat_models_with_the_key() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return models("gpt-5", "text-embedding-3-small", "whisper-1", "gpt-4.1-mini", "dall-e-3", "gpt-5")

    assert await client(handler).list_models(AiProvider.OPENAI, "sk-abc") == ["gpt-4.1-mini", "gpt-5"]
    [request] = seen
    assert str(request.url) == "https://api.openai.com/v1/models"
    assert request.headers["authorization"] == "Bearer sk-abc"


async def test_anthropic_gets_its_own_headers_and_a_big_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.anthropic.com/v1/models?limit=1000"
        assert request.headers["x-api-key"] == "sk-ant"
        assert request.headers["anthropic-version"] == "2023-06-01"
        return models("claude-sonnet-4-5")

    assert await client(handler).list_models(AiProvider.ANTHROPIC, "sk-ant") == ["claude-sonnet-4-5"]


async def test_gemini_model_names_lose_their_prefix() -> None:
    result = await client(lambda _: models("models/gemini-2.5-flash", "models/embedding-001")).list_models(
        AiProvider.GEMINI, "key"
    )
    assert result == ["gemini-2.5-flash"]


async def test_openrouter_checks_the_key_first_because_its_list_is_public() -> None:
    urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if request.url.path.endswith("/key"):
            return httpx.Response(401, json={"error": {"message": "No auth"}})
        return models("openai/gpt-5")

    with pytest.raises(AiKeyInvalidError):
        await client(handler).list_models(AiProvider.OPENROUTER, "sk-or-bad")
    assert urls == ["https://openrouter.ai/api/v1/key"]


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(401), AiKeyInvalidError),
        (httpx.Response(403), AiKeyInvalidError),
        (httpx.Response(400, json=[{"error": {"message": "API key not valid."}}]), AiKeyInvalidError),
        (httpx.Response(429), ModelRateLimitedError),
        (httpx.Response(500), AiProviderError),
        (httpx.Response(200, text="not json"), AiProviderError),
        (httpx.Response(200, json={"models": []}), AiProviderError),
    ],
)
async def test_failures(response: httpx.Response, error: type[Exception]) -> None:
    with pytest.raises(error):
        await client(lambda _: response).list_models(AiProvider.GROQ, "gsk-key")


async def test_an_unreachable_provider_is_a_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(AiProviderError):
        await client(handler).list_models(AiProvider.MISTRAL, "key")


async def test_models_their_chat_api_refuses_arent_listed() -> None:
    listed = models(
        "models/gemini-3.5-flash",
        "models/antigravity-preview-05-2026",
        "models/deep-research-max-preview-04-2026",
        "models/gemini-2.5-computer-use-preview-10-2025",
        "models/gemini-3.8-live",
        "models/gemini-3.1-flash-live-preview",
        "models/gemini-robotics-er-2-streaming-preview",
        "models/lyria-3.5",
        "models/nano-banana-pro-preview",
        "gpt-5.3-codex",
        "gpt-live-1",
        "gpt-5",
    )
    result = await client(lambda _: listed).list_models(AiProvider.GEMINI, "key")
    assert result == ["gemini-3.5-flash", "gpt-5"]


async def test_openrouter_keeps_codex_models_it_serves_through_chat() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/key"):
            return httpx.Response(200, json={"data": {}})
        return models("openai/gpt-5-codex", "openai/gpt-5")

    assert await client(handler).list_models(AiProvider.OPENROUTER, "sk-or") == ["openai/gpt-5", "openai/gpt-5-codex"]


def test_a_users_model_is_called_with_its_providers_name() -> None:
    assert provider_endpoint(AiProvider.GEMINI, "key", "gemini-3.5-flash").provider == "Google Gemini"
