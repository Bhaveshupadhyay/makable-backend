from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.ai_keys import AiProvider
from app.core.exceptions import ConflictError
from app.models.ai_key import AiKey
from app.schemas.ai_key import AiKeyStatusUpdate, AiKeyUpsert


class AiKeyRepository(Protocol):
    async def list_for_user(self, user_id: UUID) -> list[AiKey]: ...

    async def get(self, user_id: UUID, key_id: UUID) -> AiKey | None: ...

    async def get_by_provider(self, user_id: UUID, provider: AiProvider) -> AiKey | None: ...

    async def upsert(self, data: AiKeyUpsert) -> AiKey: ...

    async def set_models(self, key: AiKey, models: list[str]) -> AiKey: ...

    async def set_status(self, key: AiKey, data: AiKeyStatusUpdate) -> AiKey: ...

    async def delete(self, key: AiKey) -> None: ...


class SqlAiKeyRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_for_user(self, user_id: UUID) -> list[AiKey]:
        """The user's keys, oldest first."""
        rows = await self._session.scalars(
            select(AiKey).where(AiKey.user_id == user_id).order_by(AiKey.created_at, AiKey.id)
        )
        return list(rows)

    async def get(self, user_id: UUID, key_id: UUID) -> AiKey | None:
        """The key, if it's this user's."""
        return await self._session.scalar(select(AiKey).where(AiKey.id == key_id, AiKey.user_id == user_id))

    async def get_by_provider(self, user_id: UUID, provider: AiProvider) -> AiKey | None:
        return await self._session.scalar(select(AiKey).where(AiKey.user_id == user_id, AiKey.provider == provider))

    async def upsert(self, data: AiKeyUpsert) -> AiKey:
        """Adds the user's key for the provider, or replaces the one they have (keeping its id).

        Raises:
            ConflictError: Another request added a key for this provider at the same time. The session must then
                be rolled back.
        """
        key = await self.get_by_provider(data.user_id, data.provider)
        if key is None:
            key = AiKey(**data.model_dump())
            self._session.add(key)
        else:
            for field, value in data.model_dump(exclude={"user_id", "provider"}).items():
                setattr(key, field, value)
        try:
            await self._session.flush()
        except IntegrityError as err:
            raise ConflictError("A key for this provider was just saved. Try again.", code="ai_key_conflict") from err
        return key

    async def set_models(self, key: AiKey, models: list[str]) -> AiKey:
        key.models = list(models)
        await self._session.flush()
        return key

    async def set_status(self, key: AiKey, data: AiKeyStatusUpdate) -> AiKey:
        for field, value in data.model_dump().items():
            setattr(key, field, value)
        await self._session.flush()
        return key

    async def delete(self, key: AiKey) -> None:
        await self._session.delete(key)
        await self._session.flush()
