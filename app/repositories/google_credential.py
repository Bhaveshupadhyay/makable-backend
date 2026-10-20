from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.google_credential import GoogleCredential
from app.schemas.analytics import GoogleCredentialUpsert


class GoogleCredentialRepository(Protocol):
    async def get(self, user_id: UUID) -> GoogleCredential | None: ...

    async def upsert(self, data: GoogleCredentialUpsert) -> GoogleCredential: ...

    async def delete(self, credential: GoogleCredential) -> None: ...


class SqlGoogleCredentialRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: UUID) -> GoogleCredential | None:
        return await self._session.get(GoogleCredential, user_id)

    async def upsert(self, data: GoogleCredentialUpsert) -> GoogleCredential:
        """Adds the user's Google connection, or replaces the one they have."""
        credential = await self.get(data.user_id)
        if credential is None:
            credential = GoogleCredential(**data.model_dump())
            self._session.add(credential)
        else:
            for field, value in data.model_dump(exclude={"user_id"}).items():
                setattr(credential, field, value)
        await self._session.flush()
        return credential

    async def delete(self, credential: GoogleCredential) -> None:
        await self._session.delete(credential)
        await self._session.flush()
