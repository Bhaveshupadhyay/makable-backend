"""Users' own AI provider keys and the models they pick for the chat. The key itself only ever comes in; what goes
back out is its last few characters."""

from datetime import datetime
from typing import Annotated
from uuid import UUID

from pydantic import AfterValidator, BaseModel, BeforeValidator, Field, SecretStr

from app.constants.ai_keys import MAX_API_KEY_LENGTH, MAX_ENABLED_MODELS, MODEL_ID, AiKeyStatus, AiProvider
from app.schemas.common import CamelModel


def _model_id(value: str) -> str:
    if not MODEL_ID.fullmatch(value):
        raise ValueError("must be a model id like gpt-4.1-mini")
    return value


ModelId = Annotated[str, AfterValidator(_model_id)]


def _strip(value: object) -> object:
    return value.strip() if isinstance(value, str) else value


def _no_whitespace(value: SecretStr) -> SecretStr:
    if any(c.isspace() for c in value.get_secret_value()):
        raise ValueError("must not contain spaces")
    return value


# Pasted keys often carry a trailing newline, so the ends are trimmed first.
ApiKey = Annotated[
    SecretStr,
    BeforeValidator(_strip),
    Field(min_length=8, max_length=MAX_API_KEY_LENGTH),
    AfterValidator(_no_whitespace),
]


def _unique_models(value: list[str]) -> list[str]:
    return list(dict.fromkeys(value))


EnabledModels = Annotated[list[ModelId], Field(max_length=MAX_ENABLED_MODELS), AfterValidator(_unique_models)]


class AiProviderRead(CamelModel):
    id: AiProvider
    name: str
    # Where the user creates a key.
    keys_url: str


class AiKeyCreate(CamelModel):
    """Adds a key, or replaces the user's key for that provider."""

    provider: AiProvider
    api_key: ApiKey
    # Models to show in the chat's menu. Left out, a replaced key keeps the ones it had.
    models: EnabledModels | None = None


class AiKeyUpdate(CamelModel):
    models: EnabledModels


class AiKeyRead(CamelModel):
    id: UUID
    provider: AiProvider
    # The key's last characters.
    key_hint: str
    status: AiKeyStatus
    checked_at: datetime
    # The models shown in the chat's menu.
    models: list[str]
    created_at: datetime


class AiKeyChecked(CamelModel):
    """A key after a check, with every chat model the provider lists for it (empty if the key was refused)."""

    key: AiKeyRead
    available_models: list[str]


class AiModelOption(CamelModel):
    """One entry in the chat's model menu. `keyId` is null for makable's own model."""

    key_id: UUID | None
    provider: AiProvider | None
    model: str
    # The model reads images: the SPA sends a picture of the selected element with the request.
    accepts_images: bool = False


class ModelChoice(CamelModel):
    """The model the user picked for a request: one of their key's enabled models."""

    key_id: UUID
    model: ModelId


class AiKeyUpsert(BaseModel):
    """What's stored for a user's provider key. The key is already encrypted."""

    user_id: UUID
    provider: AiProvider
    api_key: str
    key_hint: str
    status: AiKeyStatus
    checked_at: datetime
    models: list[str]


class AiKeyStatusUpdate(BaseModel):
    status: AiKeyStatus
    checked_at: datetime


class ModelEndpoint(BaseModel):
    """Where and how to call a chat model."""

    base_url: str
    api_key: str | None
    model: str
    headers: dict[str, str] = {}
    accepts_images: bool = False
