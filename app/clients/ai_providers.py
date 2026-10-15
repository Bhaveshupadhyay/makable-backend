"""AI providers' model lists, used to check a user's key and show which models it can use. Only the fixed hosts in
`constants/ai_keys.py` are called. The key is only used for these requests and is never logged."""

import logging
from typing import Protocol

import httpx

from app.clients.model import raise_for_provider_status
from app.constants.ai_keys import (
    IMAGE_MODEL,
    KEY_CHECK_TIMEOUT_SECONDS,
    MAX_LISTED_MODELS,
    MODEL_ID,
    NOT_CHAT_MODEL,
    PROVIDERS,
    TEXT_ONLY_MODEL,
    AiProvider,
)
from app.core.client import HTTP_POOL_TIMEOUT_SECONDS
from app.core.exceptions import ExternalServiceError
from app.core.limits import ServiceBusyError
from app.schemas.ai_key import ModelEndpoint

logger = logging.getLogger(__name__)


class AiProviderError(ExternalServiceError):
    code = "ai_provider_error"
    message = "The AI provider didn't answer as expected. Try again shortly."


def model_accepts_images(model: str) -> bool:
    """Whether a model reads images, from its id (providers' model lists don't say). OpenRouter's ids carry the
    provider (`openai/gpt-4o`): the part after the slash is what counts."""
    name = model.rsplit("/", 1)[-1]
    return bool(IMAGE_MODEL.search(name)) and not TEXT_ONLY_MODEL.search(name)


def provider_endpoint(provider: AiProvider, api_key: str, model: str) -> ModelEndpoint:
    """How to call `model` at `provider` with the user's key."""
    spec = PROVIDERS[provider]
    headers = {name: value.replace("{key}", api_key) for name, value in spec.extra_headers.items()}
    return ModelEndpoint(
        base_url=spec.base_url,
        api_key=api_key,
        model=model,
        headers=headers,
        accepts_images=model_accepts_images(model),
    )


class AiProviderClient(Protocol):
    async def list_models(self, provider: AiProvider, api_key: str) -> list[str]: ...


class HttpAiProviderClient:
    """Reads providers' `/models` lists over the shared `httpx.AsyncClient`."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http

    async def list_models(self, provider: AiProvider, api_key: str) -> list[str]:
        """Checks the key and returns the chat models it can use, sorted.

        Raises:
            AiKeyInvalidError: The provider refused the key.
            ModelRateLimitedError: The provider is rate limiting the key.
            AiProviderError: The provider failed or answered something unexpected.
        """
        spec = PROVIDERS[provider]
        endpoint = provider_endpoint(provider, api_key, model="")
        headers = {**endpoint.headers, "Authorization": f"Bearer {api_key}"}
        if spec.check_path != "/models":
            # `/models` takes any key here, so a request that needs one checks it first.
            res = await self._get(provider, f"{spec.base_url}{spec.check_path}", headers, {})
            raise_for_provider_status(res)
            _check_ok(provider, res)
        res = await self._get(provider, f"{spec.base_url}/models", headers, spec.models_query)
        raise_for_provider_status(res)
        _check_ok(provider, res)
        try:
            data = res.json()
        except ValueError as err:
            raise AiProviderError() from err
        return _chat_models(data)

    async def _get(
        self, provider: AiProvider, url: str, headers: dict[str, str], params: dict[str, str]
    ) -> httpx.Response:
        try:
            return await self._http.get(
                url,
                headers=headers,
                params=params,
                timeout=httpx.Timeout(KEY_CHECK_TIMEOUT_SECONDS, pool=HTTP_POOL_TIMEOUT_SECONDS),
            )
        except httpx.PoolTimeout as err:
            raise ServiceBusyError() from err
        except httpx.HTTPError as err:
            logger.warning("AI provider unreachable", extra={"provider": provider.value, "error": repr(err)})
            raise AiProviderError() from err


def _check_ok(provider: AiProvider, res: httpx.Response) -> None:
    if res.is_error:
        logger.warning("AI provider error", extra={"provider": provider.value, "status": res.status_code})
        raise AiProviderError()


def _chat_models(data: object) -> list[str]:
    match data:
        case {"data": list(entries)}:
            pass
        case _:
            raise AiProviderError()
    ids: set[str] = set()
    for entry in entries:
        match entry:
            case {"id": str(model_id)}:
                # Gemini's OpenAI-compatible list writes `models/gemini-...`; its chat takes the bare name.
                model_id = model_id.removeprefix("models/")
                if MODEL_ID.fullmatch(model_id) and not NOT_CHAT_MODEL.search(model_id):
                    ids.add(model_id)
    return sorted(ids)[:MAX_LISTED_MODELS]
