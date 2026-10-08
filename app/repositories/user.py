from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.schemas.user import UserCreate, UserUpdate


class UserRepository(Protocol):
    async def get_by_id(self, user_id: UUID) -> User | None: ...

    async def get_by_auth_user_id(self, auth_user_id: UUID) -> User | None: ...

    async def create(self, data: UserCreate) -> User: ...

    async def update(self, user: User, data: UserUpdate) -> User: ...


class SqlUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: UUID) -> User | None:
        return await self._session.get(User, user_id)

    async def get_by_auth_user_id(self, auth_user_id: UUID) -> User | None:
        return await self._session.scalar(select(User).where(User.auth_user_id == auth_user_id))

    async def create(self, data: UserCreate) -> User:
        user = User(**data.model_dump())
        self._session.add(user)
        await self._session.flush()
        return user

    async def update(self, user: User, data: UserUpdate) -> User:
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(user, field, value)
        await self._session.flush()
        return user
