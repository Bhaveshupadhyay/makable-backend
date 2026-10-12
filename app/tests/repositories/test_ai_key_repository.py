from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.constants.ai_keys import AiKeyStatus, AiProvider
from app.core.database import utc_now
from app.core.exceptions import ConflictError
from app.models.ai_key import AiKey
from app.repositories.ai_key import SqlAiKeyRepository
from app.repositories.user import SqlUserRepository
from app.schemas.ai_key import AiKeyStatusUpdate, AiKeyUpsert
from app.schemas.user import UserCreate


async def make_user(session: AsyncSession, github_id: int) -> UUID:
    user = await SqlUserRepository(session).create(
        UserCreate(auth_user_id=uuid4(), github_id=github_id, login=f"u{github_id}", name=None, avatar_url="https://a")
    )
    return user.id


def key_for(user_id: UUID, provider: AiProvider = AiProvider.OPENAI, models: list[str] | None = None) -> AiKeyUpsert:
    return AiKeyUpsert(
        user_id=user_id,
        provider=provider,
        api_key="encrypted",
        key_hint="1234",
        status=AiKeyStatus.VALID,
        checked_at=utc_now(),
        models=models or [],
    )


async def test_keys_are_only_found_for_their_user(session: AsyncSession) -> None:
    keys = SqlAiKeyRepository(session)
    alice, bob = await make_user(session, 1), await make_user(session, 2)
    key = await keys.upsert(key_for(alice))

    assert await keys.get(alice, key.id) is key
    assert await keys.get(bob, key.id) is None
    assert await keys.list_for_user(bob) == []
    assert await keys.get_by_provider(bob, AiProvider.OPENAI) is None


async def test_upsert_replaces_the_users_key_for_a_provider(session: AsyncSession) -> None:
    keys = SqlAiKeyRepository(session)
    alice = await make_user(session, 1)
    first = await keys.upsert(key_for(alice, models=["a"]))
    other = await keys.upsert(key_for(alice, AiProvider.GROQ))

    second = await keys.upsert(key_for(alice, models=["b"]).model_copy(update={"key_hint": "9999"}))

    assert second.id == first.id
    assert (second.key_hint, second.models) == ("9999", ["b"])
    assert [k.id for k in await keys.list_for_user(alice)] == [first.id, other.id]


async def test_models_and_status_are_written_and_keys_deleted(session: AsyncSession) -> None:
    keys = SqlAiKeyRepository(session)
    alice = await make_user(session, 1)
    key = await keys.upsert(key_for(alice))

    key_id = key.id
    await keys.set_models(key, ["gpt-5"])
    await keys.set_status(key, AiKeyStatusUpdate(status=AiKeyStatus.INVALID, checked_at=utc_now()))
    await session.commit()
    session.expire_all()
    stored = await keys.get(alice, key_id)
    assert stored is not None
    assert (stored.models, stored.status) == (["gpt-5"], AiKeyStatus.INVALID)

    await keys.delete(stored)
    assert await keys.list_for_user(alice) == []


async def test_a_key_added_by_a_concurrent_request_is_a_conflict(session: AsyncSession) -> None:
    keys = SqlAiKeyRepository(session)
    alice = await make_user(session, 1)
    # Another worker's insert, which this request's lookup didn't see.
    session.add(AiKey(**key_for(alice).model_dump()))

    with pytest.raises(ConflictError) as err:
        await keys.upsert(key_for(alice))
    assert err.value.code == "ai_key_conflict"
