from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError
from app.models.user import User
from app.schemas.user import UserCreate, UserUpdate


class UserRepository(Protocol):
    async def get_by_id(self, user_id: UUID) -> User | None: ...

    async def get_by_auth_user_id(self, auth_user_id: UUID) -> User | None: ...

    async def get_by_github_id(self, github_id: int) -> User | None: ...

    async def create(self, data: UserCreate) -> User: ...

    async def update(self, user: User, data: UserUpdate) -> User: ...


class SqlUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, user_id: UUID) -> User | None:
        return await self._session.get(User, user_id)

    async def get_by_auth_user_id(self, auth_user_id: UUID) -> User | None:
        return await self._session.scalar(select(User).where(User.auth_user_id == auth_user_id))

    async def get_by_github_id(self, github_id: int) -> User | None:
        return await self._session.scalar(select(User).where(User.github_id == github_id))

    async def create(self, data: UserCreate) -> User:
        """Adds a user.

        Raises:
            ConflictError: Another user has the same `auth_user_id` or `github_id`. The session must
                then be rolled back.
        """
        user = User(**data.model_dump())
        self._session.add(user)
        await self._flush()
        return user

    async def update(self, user: User, data: UserUpdate) -> User:
        """Writes the fields that are set.

        Raises:
            ConflictError: The new `auth_user_id` belongs to another user. The session must then be
                rolled back.
        """
        for field, value in data.model_dump(exclude_unset=True).items():
            setattr(user, field, value)
        await self._flush()
        return user

    async def _flush(self) -> None:
        try:
            await self._session.flush()
        except IntegrityError as err:
            raise ConflictError("User already exists") from err
