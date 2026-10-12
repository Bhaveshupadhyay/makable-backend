"""A minimal OpenAI-compatible chat completions client: makable's own model (OmniRoute locally), or a provider with
the user's own key."""

import logging
from typing import Protocol

import httpx
from fastapi import status

from app.core.client import HTTP_POOL_TIMEOUT_SECONDS
from app.core.exceptions import AppError, ExternalServiceError
from app.core.limits import ServiceBusyError
from app.schemas.ai_key import ModelEndpoint
from app.schemas.chat import ChatMessage

logger = logging.getLogger(__name__)


class ModelUnavailableError(ExternalServiceError):
    code = "ai_unavailable"
    message = "The AI model couldn't be reached. Try again in a moment."


class AiKeyInvalidError(AppError):
    """The provider refused the user's key (401/403)."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "ai_key_invalid"
    message = "The AI provider didn't accept your API key. Check it in Settings."


class ModelRateLimitedError(AppError):
    """The provider answered 429. `details.retryAfter` is its wait in seconds."""

    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "ai_rate_limited"
    message = "The AI provider is rate limiting this key. Try again in a moment."

    def __init__(self, retry_after: int) -> None:
        super().__init__(details={"retryAfter": retry_after}, headers={"Retry-After": str(retry_after)})


def raise_for_provider_status(res: httpx.Response) -> None:
    """Turns a provider's refusal of the key, or its rate limit, into a domain error.

    Raises:
        AiKeyInvalidError: 401 or 403.
        ModelRateLimitedError: 429.
    """
    if res.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN):
        raise AiKeyInvalidError()
    if res.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        raise ModelRateLimitedError(_retry_after(res))


def _retry_after(res: httpx.Response) -> int:
    try:
        return max(1, min(int(float(res.headers.get("retry-after", ""))), 3600))
    except ValueError:
        return 20


class ModelClient(Protocol):
    @property
    def name(self) -> str: ...

    async def complete(self, messages: list[ChatMessage]) -> str: ...


class HttpModelClient:
    """Calls `<base_url>/chat/completions` over a shared `httpx.AsyncClient`."""

    def __init__(self, http: httpx.AsyncClient, endpoint: ModelEndpoint, *, timeout_seconds: float) -> None:
        self._http = http
        self._url = f"{endpoint.base_url}/chat/completions"
        self._headers = dict(endpoint.headers)
        if endpoint.api_key:
            self._headers["Authorization"] = f"Bearer {endpoint.api_key}"
        self._model = endpoint.model
        self._timeout = timeout_seconds

    @property
    def name(self) -> str:
        return self._model

    async def complete(self, messages: list[ChatMessage]) -> str:
        """Returns the reply's text.

        Raises:
            AiKeyInvalidError: The provider refused the key.
            ModelRateLimitedError: The provider is rate limiting the key.
            ModelUnavailableError: The model couldn't be reached, answered with an error, or sent no text.
        """
        body = {
            "model": self._model,
            "messages": [m.model_dump() for m in messages],
            "temperature": 0.2,
            "stream": False,
        }
        try:
            res = await self._http.post(
                self._url,
                json=body,
                headers=self._headers,
                timeout=httpx.Timeout(self._timeout, pool=HTTP_POOL_TIMEOUT_SECONDS),
            )
        except httpx.PoolTimeout as err:
            raise ServiceBusyError() from err
        except httpx.HTTPError as err:
            logger.warning("model unreachable", extra={"url": self._url, "error": repr(err)})
            raise ModelUnavailableError() from err
        raise_for_provider_status(res)
        try:
            data = res.json()
        except ValueError:
            data = None
        content = _content(data)
        if res.is_error or content is None:
            logger.warning("model call failed", extra={"status": res.status_code, "error": _error_message(data)})
            raise ModelUnavailableError()
        return content


def _content(data: object) -> str | None:
    match data:
        case {"choices": [{"message": {"content": str(content)}}, *_]}:
            return content
    return None


def _error_message(data: object) -> str | None:
    match data:
        case {"error": {"message": str(message)}}:
            return message
    return None
