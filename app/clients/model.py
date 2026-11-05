"""A minimal OpenAI-compatible chat completions client: makable's own model (OmniRoute locally), or a provider with
the user's own key."""

import logging
import re
from collections.abc import Sequence
from typing import Protocol

import httpx
from fastapi import status

from app.core.client import HTTP_POOL_TIMEOUT_SECONDS
from app.core.exceptions import AppError, ExternalServiceError
from app.core.limits import ServiceBusyError
from app.schemas.ai_key import ModelEndpoint
from app.schemas.chat import ChatMessage

logger = logging.getLogger(__name__)

# How much of a provider's error message is logged, shown to the user and kept with the request.
MAX_PROVIDER_MESSAGE_CHARS = 400
# Credentials a provider's message could echo, taken out of it: keys as providers write them (OpenAI, Anthropic,
# OpenRouter and DeepSeek `sk-`, Groq `gsk_`, xAI `xai-`, Gemini `AIza`), bearer tokens, and keys in URLs.
_KEY_LIKE = re.compile(r"\b(?:sk|gsk|xai)[-_][\w*.-]{8,}|\bAIza[\w-]{20,}|\bBearer\s+[^\s\"',]+", re.IGNORECASE)
_KEY_PARAM = re.compile(r"([?&](?:key|api_key|access_token)=)[^&\s\"']+", re.IGNORECASE)
REDACTED = "[redacted]"
# Shorter header values aren't taken out of messages (they'd match ordinary words).
MIN_CREDENTIAL_CHARS = 8


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

    def __init__(self, retry_after: int, message: str | None = None) -> None:
        super().__init__(message, details={"retryAfter": retry_after}, headers={"Retry-After": str(retry_after)})


def raise_for_provider_status(res: httpx.Response, *, provider: str | None = None, model: str = "") -> None:
    """Turns a provider's refusal of the key, or its rate limit, into a domain error. With `provider` (the user's own
    provider), the rate limit is told in the provider's words: a 429 is also how OpenAI and Gemini say the account has
    no credit or quota left, which waiting doesn't fix.

    Raises:
        AiKeyInvalidError: 401 or 403, or a 400 about the API key (Gemini's answer to a bad key).
        ModelRateLimitedError: 429.
    """
    if res.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN):
        raise AiKeyInvalidError()
    if res.status_code == status.HTTP_400_BAD_REQUEST and "api key" in res.text.lower():
        raise AiKeyInvalidError()
    if res.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        said = _provider_said(res)
        logger.warning("AI provider rate limited", extra={"model": model or None, "error": said})
        message = _provider_failed(provider, model, res.status_code, said) if provider is not None else None
        raise ModelRateLimitedError(_retry_after(res), message)


def _retry_after(res: httpx.Response) -> int:
    try:
        return max(1, min(int(float(res.headers.get("retry-after", ""))), 3600))
    except ValueError:
        return 20


class ModelClient(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def accepts_images(self) -> bool: ...

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
        self._provider = endpoint.provider
        self._accepts_images = endpoint.accepts_images
        self._timeout = timeout_seconds

    @property
    def name(self) -> str:
        return self._model

    @property
    def accepts_images(self) -> bool:
        return self._accepts_images

    async def complete(self, messages: list[ChatMessage]) -> str:
        """Returns the reply's text.

        A user's own provider's failures are told with its name, and in its own words when it answered with an error:
        what to do about them (add credit, pick another model, wait) is between the user and their provider.

        Raises:
            AiKeyInvalidError: The provider refused the key.
            ModelRateLimitedError: The provider is rate limiting the key.
            ModelUnavailableError: The model couldn't be reached, answered with an error, or sent no text.
        """
        body = {
            "model": self._model,
            "messages": [m.model_dump(exclude_none=True) for m in messages],
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
            logger.warning("model unreachable", extra={"url": self._url, "model": self._model, "error": repr(err)})
            raise ModelUnavailableError(self._unreachable(err)) from err
        raise_for_provider_status(res, provider=self._provider, model=self._model)
        try:
            data = res.json()
        except ValueError:
            data = None
        content = _content(data)
        if res.is_error or content is None:
            said = _error_message(data, _sent_credentials(res))
            logger.warning("model call failed", extra={"status": res.status_code, "model": self._model, "error": said})
            message = None
            if self._provider is not None:
                message = (
                    _provider_failed(self._provider, self._model, res.status_code, said)
                    if res.is_error
                    else f"Your AI provider ({self._provider}) sent an empty reply from {self._model}."
                )
            raise ModelUnavailableError(message)
        return content

    def _unreachable(self, err: httpx.HTTPError) -> str | None:
        """What the user is told when their provider didn't answer; None (the default message) for makable's own."""
        if self._provider is None:
            return None
        if isinstance(err, httpx.TimeoutException):
            return (
                f"{self._model} at your AI provider ({self._provider}) didn't answer within {self._timeout:g} seconds."
                " Try again, or pick another model."
            )
        return f"makable couldn't reach your AI provider ({self._provider}). Try again in a moment."


def _content(data: object) -> str | None:
    match data:
        case {"choices": [{"message": {"content": str(content)}}, *_]}:
            return content
    return None


def _error_message(data: object, credentials: Sequence[str] = ()) -> str | None:
    """The provider's error message, on one line, without `credentials` (what the request sent) or anything shaped
    like one, and cut to `MAX_PROVIDER_MESSAGE_CHARS`. It's logged, shown to the user and kept with the request."""
    match data:
        # Gemini's OpenAI-compatible API wraps the error in a list.
        case {"error": {"message": str(message)}} | [{"error": {"message": str(message)}}, *_]:
            message = " ".join(message.split())
            for credential in credentials:
                message = message.replace(credential, REDACTED)
            message = _KEY_PARAM.sub(rf"\1{REDACTED}", _KEY_LIKE.sub(REDACTED, message))
            if len(message) > MAX_PROVIDER_MESSAGE_CHARS:
                message = message[: MAX_PROVIDER_MESSAGE_CHARS - 1].rstrip() + "…"
            return message or None
    return None


def _provider_said(res: httpx.Response) -> str | None:
    try:
        return _error_message(res.json(), _sent_credentials(res))
    except ValueError:
        return None


def _sent_credentials(res: httpx.Response) -> list[str]:
    """The key the request was sent with (as a bearer token, or Anthropic's `x-api-key`), whatever its shape."""
    try:
        headers = res.request.headers
    except RuntimeError:  # A response that wasn't sent by a client: nothing was.
        return []
    sent = [headers.get("authorization", "").removeprefix("Bearer ").strip(), headers.get("x-api-key", "").strip()]
    return [value for value in sent if len(value) >= MIN_CREDENTIAL_CHARS]


def _provider_failed(provider: str, model: str, status_code: int, said: str | None) -> str:
    if said is None:
        return f"Your AI provider ({provider}) returned an error for {model} (HTTP {status_code})."
    return f"Your AI provider ({provider}) returned an error for {model}: {said}"
