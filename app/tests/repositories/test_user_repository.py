from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.auth import Role
from app.core.exceptions import ConflictError
from app.repositories.user import SqlUserRepository
from app.schemas.user import UserCreate, UserUpdate

AUTH_USER_ID = uuid4()


def octocat() -> UserCreate:
    return UserCreate(
        auth_user_id=AUTH_USER_ID,
        github_id=1,
        login="octocat",
        name="The Octocat",
        avatar_url="https://a.example/o.png",
    )


async def test_create_and_find(session: AsyncSession) -> None:
    users = SqlUserRepository(session)
    created = await users.create(octocat())

    assert created.role is Role.USER
    assert await users.get_by_id(created.id) is created
    assert await users.get_by_auth_user_id(AUTH_USER_ID) is created
    assert await users.get_by_auth_user_id(uuid4()) is None
    assert await users.get_by_id(uuid4()) is None


async def test_duplicates_raise_conflict(session: AsyncSession) -> None:
    users = SqlUserRepository(session)
    await users.create(octocat())

    with pytest.raises(ConflictError):
        await users.create(octocat().model_copy(update={"auth_user_id": uuid4()}))


async def test_update_only_writes_set_fields(session: AsyncSession) -> None:
    users = SqlUserRepository(session)
    user = await users.create(octocat())

    await users.update(user, UserUpdate(login="renamed"))

    assert user.login == "renamed"
    assert user.name == "The Octocat"


async def test_never_commits(session: AsyncSession) -> None:
    users = SqlUserRepository(session)
    await users.create(octocat())
    await session.rollback()

    assert await users.get_by_auth_user_id(AUTH_USER_ID) is None
