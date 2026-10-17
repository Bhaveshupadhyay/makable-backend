import logging
from collections.abc import Callable
from uuid import UUID

from fastapi import status

from app.clients.ai_providers import AiProviderClient, model_accepts_images, provider_endpoint
from app.clients.model import AiKeyInvalidError, ModelClient
from app.constants.ai_keys import KEY_HINT_CHARS, PROVIDERS, AiKeyStatus
from app.core.database import utc_now
from app.core.exceptions import AppError, NotFoundError
from app.core.security import TokenCipher
from app.models.ai_key import AiKey
from app.repositories.ai_key import AiKeyRepository
from app.repositories.unit_of_work import UnitOfWork
from app.schemas.ai_key import (
    AiKeyChecked,
    AiKeyCreate,
    AiKeyRead,
    AiKeyStatusUpdate,
    AiKeyUpdate,
    AiKeyUpsert,
    AiModelOption,
    AiProviderRead,
    ModelChoice,
    ModelEndpoint,
)

logger = logging.getLogger(__name__)

ModelClientFactory = Callable[[ModelEndpoint], ModelClient]


class AiKeyNotFoundError(NotFoundError):
    code = "ai_key_not_found"
    message = "That API key isn't in your settings any more."


class AiModelNotEnabledError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "ai_model_not_enabled"
    message = "That model isn't turned on for your API key. Pick it in Settings."


class AiModelRequiredError(AppError):
    """There's no makable model (`AI_BASE_URL` unset), and the request didn't pick one of the user's."""

    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "ai_model_required"
    message = "Add your own API key in Settings to use AI edits."


class AiKeyService:
    """Users' own AI provider keys: checked live with the provider before they're kept, stored encrypted, and turned
    into the model client for a request."""

    def __init__(
        self,
        keys: AiKeyRepository,
        providers: AiProviderClient,
        cipher: TokenCipher,
        uow: UnitOfWork,
        *,
        default_model: ModelClient | None,
        model_client: ModelClientFactory,
    ) -> None:
        self._keys = keys
        self._providers = providers
        self._cipher = cipher
        self._uow = uow
        self._default_model = default_model
        self._model_client = model_client

    @staticmethod
    def providers() -> list[AiProviderRead]:
        return [AiProviderRead(id=p, name=spec.name, keys_url=spec.keys_url) for p, spec in PROVIDERS.items()]

    async def list_keys(self, user_id: UUID) -> list[AiKeyRead]:
        return [AiKeyRead.model_validate(k) for k in await self._keys.list_for_user(user_id)]

    async def add_key(self, user_id: UUID, data: AiKeyCreate) -> AiKeyChecked:
        """Checks the key with the provider and keeps it, replacing the user's key for that provider. Only models
        the key can use are kept enabled.

        Raises:
            AiKeyInvalidError: The provider refused the key. Nothing is stored.
            ModelRateLimitedError: The provider is rate limiting the key. Nothing is stored.
            AiProviderError: The provider failed. Nothing is stored.
            ConflictError: Another request added a key for this provider at the same time.
        """
        api_key = data.api_key.get_secret_value()
        available = await self._providers.list_models(data.provider, api_key)
        if data.models is not None:
            wanted = data.models
        else:
            existing = await self._keys.get_by_provider(user_id, data.provider)
            wanted = existing.models if existing else []
        usable = set(available)
        key = await self._keys.upsert(
            AiKeyUpsert(
                user_id=user_id,
                provider=data.provider,
                api_key=self._cipher.encrypt(api_key),
                key_hint=api_key[-KEY_HINT_CHARS:],
                status=AiKeyStatus.VALID,
                checked_at=utc_now(),
                models=[m for m in wanted if m in usable],
            )
        )
        result = AiKeyChecked(key=AiKeyRead.model_validate(key), available_models=available)
        await self._uow.commit()
        return result

    async def update_key(self, user_id: UUID, key_id: UUID, data: AiKeyUpdate) -> AiKeyRead:
        """Sets the models the key shows in the chat's menu.

        Raises:
            AiKeyNotFoundError: The user has no such key.
        """
        key = await self._get(user_id, key_id)
        result = AiKeyRead.model_validate(await self._keys.set_models(key, data.models))
        await self._uow.commit()
        return result

    async def check_key(self, user_id: UUID, key_id: UUID) -> AiKeyChecked:
        """Asks the provider again whether the key works, and records the answer. A refused key is marked invalid
        rather than raised (its models are kept, for when it's replaced); a working key drops models the provider
        no longer lists.

        Raises:
            AiKeyNotFoundError: The user has no such key.
            ModelRateLimitedError: The provider is rate limiting the key.
            AiProviderError: The provider failed. The key's status is left as it was.
        """
        key = await self._get(user_id, key_id)
        api_key = self._cipher.decrypt(key.api_key)
        try:
            available = await self._providers.list_models(key.provider, api_key)
        except AiKeyInvalidError:
            available = []
            key_status = AiKeyStatus.INVALID
        else:
            key_status = AiKeyStatus.VALID
            usable = set(available)
            key = await self._keys.set_models(key, [m for m in key.models if m in usable])
        key = await self._keys.set_status(key, AiKeyStatusUpdate(status=key_status, checked_at=utc_now()))
        result = AiKeyChecked(key=AiKeyRead.model_validate(key), available_models=available)
        await self._uow.commit()
        return result

    async def delete_key(self, user_id: UUID, key_id: UUID) -> None:
        """Forgets the key.

        Raises:
            AiKeyNotFoundError: The user has no such key.
        """
        await self._keys.delete(await self._get(user_id, key_id))
        await self._uow.commit()

    async def model_options(self, user_id: UUID) -> list[AiModelOption]:
        """The chat's model menu: makable's own model first (if there is one), then the enabled models of the user's
        working keys."""
        options = []
        if self._default_model is not None:
            default = self._default_model
            options.append(
                AiModelOption(key_id=None, provider=None, model=default.name, accepts_images=default.accepts_images)
            )
        for key in await self._keys.list_for_user(user_id):
            if key.status == AiKeyStatus.VALID:
                options += [
                    AiModelOption(key_id=key.id, provider=key.provider, model=m, accepts_images=model_accepts_images(m))
                    for m in key.models
                ]
        return options

    async def model_for(self, user_id: UUID, choice: ModelChoice | None) -> ModelClient:
        """The model to answer a request: the user's pick, or makable's own model when there's none.

        It also ends the request's database transaction: the model can take a minute to answer, and the request
        mustn't hold a pooled connection all that time.

        Raises:
            AiModelRequiredError: Nothing was picked and there's no makable model.
            AiKeyNotFoundError: The user has no such key.
            AiKeyInvalidError: The key was refused the last time it was used or checked.
            AiModelNotEnabledError: The model isn't one the user turned on for the key.
        """
        try:
            if choice is None:
                if self._default_model is None:
                    raise AiModelRequiredError()
                return self._default_model
            key = await self._get(user_id, choice.key_id)
            if key.status == AiKeyStatus.INVALID:
                raise AiKeyInvalidError()
            if choice.model not in key.models:
                raise AiModelNotEnabledError()
            endpoint = provider_endpoint(key.provider, self._cipher.decrypt(key.api_key), choice.model)
            return self._model_client(endpoint)
        finally:
            await self._uow.rollback()

    async def key_refused(self, user_id: UUID, key_id: UUID) -> None:
        """Marks a key invalid after the provider refused it during a request, so the menu stops offering it."""
        key = await self._keys.get(user_id, key_id)
        if key is None:
            return
        await self._keys.set_status(key, AiKeyStatusUpdate(status=AiKeyStatus.INVALID, checked_at=utc_now()))
        await self._uow.commit()
        logger.info("AI key refused by the provider", extra={"user_id": str(user_id), "provider": key.provider.value})

    async def _get(self, user_id: UUID, key_id: UUID) -> AiKey:
        key = await self._keys.get(user_id, key_id)
        if key is None:
            raise AiKeyNotFoundError()
        return key
