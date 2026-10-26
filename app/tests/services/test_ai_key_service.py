"""Picking a request's model and marking refused keys, on the test database."""

from datetime import timedelta

from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.model import ModelClient
from app.constants.ai_keys import AiKeyStatus, AiProvider
from app.core.database import utc_now
from app.core.security import TokenCipher
from app.repositories.ai_key import SqlAiKeyRepository
from app.repositories.unit_of_work import SqlAlchemyUnitOfWork
from app.schemas.ai_key import AiKeyCreate, ModelChoice, ModelEndpoint
from app.services.ai_key_service import AiKeyService
from app.tests.ai_edit_fixtures import USER
from app.tests.fakes import VALID_AI_KEY, FakeAiProviders, FakeModelClient


def key_service(session: AsyncSession, built: list[str]) -> AiKeyService:
    """The service, building a fake model for each endpoint (recorded in `built`)."""

    def factory(endpoint: ModelEndpoint) -> ModelClient:
        built.append(endpoint.model)
        return FakeModelClient("hi", name=endpoint.model)

    return AiKeyService(
        SqlAiKeyRepository(session),
        FakeAiProviders(),
        TokenCipher(Fernet.generate_key().decode()),
        SqlAlchemyUnitOfWork(session),
        default_model=None,
        model_client=factory,
    )


def new_key(provider: AiProvider, models: list[str]) -> AiKeyCreate:
    return AiKeyCreate.model_validate({"provider": provider, "apiKey": VALID_AI_KEY, "models": models})


async def test_a_fallback_is_built_only_when_the_request_switches_to_it(session: AsyncSession) -> None:
    built: list[str] = []
    service = key_service(session, built)
    picked = await service.add_key(USER.id, new_key(AiProvider.OPENAI, ["gpt-5", "o4-mini"]))
    other = await service.add_key(USER.id, new_key(AiProvider.GROQ, ["gpt-4.1-mini"]))
    # The other key can't be decrypted any more (e.g. written with an older encryption key).
    row = await SqlAiKeyRepository(session).get(USER.id, other.key.id)
    assert row is not None
    row.api_key = "not-a-fernet-token"
    await session.commit()

    model = await service.model_for(USER.id, ModelChoice(key_id=picked.key.id, model="gpt-5"))

    assert await model.complete([]) == "hi"
    assert built == ["gpt-5"]


async def test_a_refused_key_is_marked_invalid_unless_it_was_checked_since(session: AsyncSession) -> None:
    service = key_service(session, [])
    added = await service.add_key(USER.id, new_key(AiProvider.OPENAI, ["gpt-5"]))
    key_id = added.key.id
    checked_at = added.key.checked_at

    # A run that read the key before it was checked (or replaced) again: its refusal was for the old key.
    await service.key_refused(USER.id, key_id, since=checked_at - timedelta(seconds=1))
    assert (await service.list_keys(USER.id))[0].status == AiKeyStatus.VALID

    await service.key_refused(USER.id, key_id, since=utc_now())
    assert (await service.list_keys(USER.id))[0].status == AiKeyStatus.INVALID
