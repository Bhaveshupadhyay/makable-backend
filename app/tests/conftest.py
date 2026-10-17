from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession

from app import models  # noqa: F401  # registers every table
from app.api import dependencies
from app.api.dependencies import (
    get_ai_provider_client,
    get_deploy_platform_client,
    get_github_client,
    get_supabase_client,
)
from app.constants.ai_keys import KEY_CHECK_BURST, KEY_CHECK_EVERY_SECONDS
from app.constants.api import (
    MAX_CONCURRENT_AI_EDITS,
    MAX_CONCURRENT_CREATIONS,
    MAX_CONCURRENT_PUBLISH_REQUESTS,
    MAX_CONCURRENT_SITE_REQUESTS,
    PUBLISH_BURST,
    PUBLISH_EVERY_SECONDS,
    SITE_SAVE_BURST,
    SITE_SAVE_EVERY_SECONDS,
)
from app.core.client import close_connection, get_postgres_client, open_connection
from app.core.config import Settings, get_settings
from app.core.database import Base
from app.core.limits import ConcurrencyLimit, KeyLock, PerKeyLimit
from app.models.site import Site
from app.repositories.site import SqlAiEditRecordRepository, SqlSiteRepository
from app.repositories.unit_of_work import SqlAlchemyUnitOfWork
from app.services.ai_edit_service import AiEditService, AiRunInProgressError, ModelSource
from app.services.site_create_service import SiteCreateService
from app.services.site_service import VERIFIED_SITES, SiteService
from app.services.site_snapshots import SITE_SNAPSHOTS
from app.tests.ai_edit_fixtures import PROJECT_ID, REPO_NAME, USER, MakeAiEditService, add_site_repo
from app.tests.fakes import (
    FAKE_CIPHER,
    FakeAiProviders,
    FakeCredentials,
    FakeDeployPlatforms,
    FakeGithubClient,
    FakeSupabaseClient,
)
from main import create_app

APP_URL = "http://localhost:5173"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """A fresh SQLite file with the schema. Tests use Base.metadata; `alembic check` keeps migrations in sync."""
    path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return path


@pytest.fixture
def settings(db_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        environment="test",
        log_level="WARNING",
        app_url=APP_URL,
        database_url=f"sqlite+aiosqlite:///{db_path}",
        secret_key=SecretStr("test-secret-that-is-at-least-32-chars"),
        token_encryption_key=SecretStr(Fernet.generate_key().decode()),
        supabase_url="https://project.supabase.co",
        supabase_publishable_key="sb_publishable_test",
    )


@pytest.fixture(autouse=True)
def reset_process_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The caches and the request limits are process-wide; tests mustn't see each other's."""
    VERIFIED_SITES.clear()
    SITE_SNAPSHOTS.clear()
    monkeypatch.setattr(dependencies, "PUBLISH_SLOTS", ConcurrencyLimit(MAX_CONCURRENT_PUBLISH_REQUESTS))
    monkeypatch.setattr(dependencies, "PUBLISHES", PerKeyLimit(burst=PUBLISH_BURST, every=PUBLISH_EVERY_SECONDS))
    monkeypatch.setattr(
        dependencies, "AI_KEY_CHECKS", PerKeyLimit(burst=KEY_CHECK_BURST, every=KEY_CHECK_EVERY_SECONDS)
    )
    monkeypatch.setattr(dependencies, "AI_EDIT_SLOTS", ConcurrencyLimit(MAX_CONCURRENT_AI_EDITS))
    monkeypatch.setattr(dependencies, "AI_CREATE_SLOTS", ConcurrencyLimit(MAX_CONCURRENT_CREATIONS))
    monkeypatch.setattr(dependencies, "AI_RUNS", KeyLock(AiRunInProgressError))
    monkeypatch.setattr(dependencies, "SITE_SLOTS", ConcurrencyLimit(MAX_CONCURRENT_SITE_REQUESTS))
    monkeypatch.setattr(dependencies, "SITE_SAVES", PerKeyLimit(burst=SITE_SAVE_BURST, every=SITE_SAVE_EVERY_SECONDS))
    yield


@pytest.fixture(autouse=True)
async def reset_postgres_client() -> AsyncIterator[None]:
    """The client is a process-wide singleton; without this a test could reuse the previous test's engine."""
    yield
    await close_connection()


@pytest.fixture
async def session(settings: Settings) -> AsyncIterator[AsyncSession]:
    await open_connection(settings)
    async with get_postgres_client()() as session:
        yield session


@pytest.fixture
def checkpointer() -> InMemorySaver:
    return InMemorySaver()


@pytest.fixture
async def site(session: AsyncSession, github: FakeGithubClient) -> str:
    """`USER`'s site (`PROJECT_ID`): its row, and its repo on the fake GitHub. Returns the commit it's at."""
    base = add_site_repo(github)
    session.add(Site(id=PROJECT_ID, user_id=USER.id, name="My site", repo_name=REPO_NAME))
    await session.commit()
    return base


def make_site_service(session: AsyncSession, github: FakeGithubClient) -> SiteService:
    return SiteService(
        github, SqlSiteRepository(session), FakeCredentials(), FAKE_CIPHER, SqlAlchemyUnitOfWork(session)
    )


@pytest.fixture
def ai_edit_service(
    session: AsyncSession, checkpointer: InMemorySaver, github: FakeGithubClient, site: str
) -> MakeAiEditService:
    """Builds `AiEditService` on the test database, the fake GitHub (with `USER`'s site) and one checkpointer per
    test, so two services made in a test are two graph instances on the same saved threads (a closed tab, a
    restarted server)."""
    runs = KeyLock(AiRunInProgressError)

    def make(model_source: ModelSource, *, debug: bool = False) -> AiEditService:
        sites = SqlSiteRepository(session)
        return AiEditService(
            model_source,
            make_site_service(session, github),
            sites,
            SqlAiEditRecordRepository(session),
            SqlAlchemyUnitOfWork(session),
            checkpointer,
            runs,
            debug=debug,
        )

    return make


@dataclass
class AiServices:
    """The AI services on the test database, the fake GitHub and one checkpointer, sharing the site lock: for tests
    where the site doesn't exist yet (creations)."""

    edit: Callable[[ModelSource], AiEditService]
    create: Callable[[ModelSource], SiteCreateService]


@pytest.fixture
def ai_services(session: AsyncSession, checkpointer: InMemorySaver, github: FakeGithubClient) -> AiServices:
    runs = KeyLock(AiRunInProgressError)

    def edit(model_source: ModelSource) -> AiEditService:
        return AiEditService(
            model_source,
            make_site_service(session, github),
            SqlSiteRepository(session),
            SqlAiEditRecordRepository(session),
            SqlAlchemyUnitOfWork(session),
            checkpointer,
            runs,
        )

    def create(model_source: ModelSource) -> SiteCreateService:
        return SiteCreateService(
            model_source,
            make_site_service(session, github),
            SqlSiteRepository(session),
            SqlAiEditRecordRepository(session),
            SqlAlchemyUnitOfWork(session),
            checkpointer,
            runs,
            heartbeat=0.05,
        )

    return AiServices(edit=edit, create=create)


@pytest.fixture
def github() -> FakeGithubClient:
    return FakeGithubClient()


@pytest.fixture
def supabase() -> FakeSupabaseClient:
    return FakeSupabaseClient()


@pytest.fixture
def platforms() -> FakeDeployPlatforms:
    return FakeDeployPlatforms()


@pytest.fixture
def ai_providers() -> FakeAiProviders:
    return FakeAiProviders()


@pytest.fixture
def client(
    settings: Settings,
    ai_providers: FakeAiProviders,
    github: FakeGithubClient,
    supabase: FakeSupabaseClient,
    platforms: FakeDeployPlatforms,
) -> Iterator[TestClient]:
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_github_client] = lambda: github
    app.dependency_overrides[get_supabase_client] = lambda: supabase
    app.dependency_overrides[get_deploy_platform_client] = lambda: platforms
    app.dependency_overrides[get_ai_provider_client] = lambda: ai_providers
    # The SPA's origin: in dev the Vite proxy serves the API under it.
    with TestClient(app, base_url=APP_URL, follow_redirects=False) as client:
        yield client
