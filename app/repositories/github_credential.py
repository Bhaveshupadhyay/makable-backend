from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.github_credential import GithubCredential
from app.schemas.github import GithubCredentialUpsert


class GithubCredentialRepository(Protocol):
    async def get(self, user_id: UUID) -> GithubCredential | None: ...

    async def upsert(self, data: GithubCredentialUpsert) -> GithubCredential: ...


class SqlGithubCredentialRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: UUID) -> GithubCredential | None:
        return await self._session.get(GithubCredential, user_id)

    async def upsert(self, data: GithubCredentialUpsert) -> GithubCredential:
        credential = await self.get(data.user_id)
        if credential is None:
            credential = GithubCredential(**data.model_dump())
            self._session.add(credential)
        else:
            for field, value in data.model_dump(exclude={"user_id"}).items():
                setattr(credential, field, value)
        await self._session.flush()
        return credential
