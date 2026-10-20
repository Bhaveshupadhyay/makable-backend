"""A minimal OpenAI-compatible chat completions client: makable's own model (OmniRoute locally), or a provider with
the user's own key."""

import logging
import time
from dataclasses import dataclass
from typing import Protocol, TypeGuard

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
        AiKeyInvalidError: 401 or 403, or a 400 about the API key (Gemini's answer to a bad key).
        ModelRateLimitedError: 429.
    """
    if res.status_code in (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN):
        raise AiKeyInvalidError()
    if res.status_code == status.HTTP_400_BAD_REQUEST and "api key" in res.text.lower():
        raise AiKeyInvalidError()
    if res.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        raise ModelRateLimitedError(_retry_after(res))


def _retry_after(res: httpx.Response) -> int:
    try:
        return max(1, min(int(float(res.headers.get("retry-after", ""))), 3600))
    except ValueError:
        return 20


@dataclass(frozen=True)
class TokenUsage:
    """The tokens a reply took, as the provider counted them. `output` includes the model's thinking; `reasoning` is
    how much of it was thinking and `cached` how much of `input` came from the provider's cache, when the provider
    says. `cost_usd` is what the provider charged, from those that tell (OpenRouter)."""

    input: int
    output: int
    reasoning: int | None = None
    cached: int | None = None
    cost_usd: float | None = None


@dataclass(frozen=True)
class ModelReply:
    text: str
    # None when the provider didn't say how many tokens the reply took.
    usage: TokenUsage | None = None
    # How long the call took.
    seconds: float = 0.0


class ModelClient(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def accepts_images(self) -> bool: ...

    async def complete(self, messages: list[ChatMessage]) -> ModelReply: ...


class HttpModelClient:
    """Calls `<base_url>/chat/completions` over a shared `httpx.AsyncClient`."""

    def __init__(self, http: httpx.AsyncClient, endpoint: ModelEndpoint, *, timeout_seconds: float) -> None:
        self._http = http
        self._url = f"{endpoint.base_url}/chat/completions"
        self._headers = dict(endpoint.headers)
        if endpoint.api_key:
            self._headers["Authorization"] = f"Bearer {endpoint.api_key}"
        self._model = endpoint.model
        self._accepts_images = endpoint.accepts_images
        self._timeout = timeout_seconds

    @property
    def name(self) -> str:
        return self._model

    @property
    def accepts_images(self) -> bool:
        return self._accepts_images

    async def complete(self, messages: list[ChatMessage]) -> ModelReply:
        """Returns the reply's text, the tokens it took and how long it took.

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
        started = time.perf_counter()
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
        return ModelReply(content, _usage(data), round(time.perf_counter() - started, 3))


def _content(data: object) -> str | None:
    match data:
        case {"choices": [{"message": {"content": str(content)}}, *_]}:
            return content
    return None


def _usage(data: object) -> TokenUsage | None:
    """The reply's `usage`, which every provider sends in OpenAI's shape, each with its own extras. Thinking that a
    provider counts in `total_tokens` but not in `completion_tokens` (Gemini) is added to the output, as thinking."""
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return None
    prompt, completion, total = usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("total_tokens")
    if not _is_count(prompt) or not _is_count(completion):
        return None
    hidden = max(0, total - prompt - completion) if _is_count(total) else 0
    reasoning = _count(usage.get("completion_tokens_details"), "reasoning_tokens")
    if hidden:
        reasoning = max(reasoning or 0, hidden)
    cached = _count(usage.get("prompt_tokens_details"), "cached_tokens")
    if cached is None:
        cached = _count(usage, "prompt_cache_hit_tokens")  # DeepSeek's own name
    # OpenRouter's, in its credits (dollars).
    cost = usage.get("cost")
    cost_usd = float(cost) if isinstance(cost, int | float) and not isinstance(cost, bool) and cost >= 0 else None
    return TokenUsage(prompt, completion + hidden, reasoning, cached, cost_usd)


def _is_count(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _count(details: object, key: str) -> int | None:
    value = details.get(key) if isinstance(details, dict) else None
    return value if _is_count(value) else None


def _error_message(data: object) -> str | None:
    match data:
        # Gemini's OpenAI-compatible API wraps the error in a list.
        case {"error": {"message": str(message)}} | [{"error": {"message": str(message)}}, *_]:
            return message[:500]
    return None
