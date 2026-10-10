"""A minimal OpenAI-compatible chat completions client (OmniRoute locally)."""

import logging
from typing import Protocol

import httpx

from app.core.client import HTTP_POOL_TIMEOUT_SECONDS
from app.core.exceptions import ExternalServiceError
from app.core.limits import ServiceBusyError
from app.schemas.chat import ChatMessage

logger = logging.getLogger(__name__)


class ModelUnavailableError(ExternalServiceError):
    code = "ai_unavailable"
    message = "The AI model couldn't be reached. Try again in a moment."


class ModelClient(Protocol):
    @property
    def name(self) -> str: ...

    async def complete(self, messages: list[ChatMessage]) -> str: ...


class HttpModelClient:
    """Calls `<base_url>/chat/completions` over a shared `httpx.AsyncClient`."""

    def __init__(
        self, http: httpx.AsyncClient, *, base_url: str, api_key: str | None, model: str, timeout_seconds: float
    ) -> None:
        self._http = http
        self._url = f"{base_url}/chat/completions"
        self._api_key = api_key
        self._model = model
        self._timeout = timeout_seconds

    @property
    def name(self) -> str:
        return self._model

    async def complete(self, messages: list[ChatMessage]) -> str:
        """Returns the reply's text.

        Raises:
            ModelUnavailableError: The model couldn't be reached, answered with an error, or sent no text.
        """
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
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
                headers=headers,
                timeout=httpx.Timeout(self._timeout, pool=HTTP_POOL_TIMEOUT_SECONDS),
            )
        except httpx.PoolTimeout as err:
            raise ServiceBusyError() from err
        except httpx.HTTPError as err:
            logger.warning("model unreachable", extra={"url": self._url, "error": repr(err)})
            raise ModelUnavailableError() from err
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
