from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine
from sqlalchemy.ext.asyncio import AsyncSession

from app import models  # noqa: F401  # registers every table
from app.api.dependencies import get_github_client, get_supabase_client
from app.core.client import close_connection, get_postgres_client, open_connection
from app.core.config import Settings, get_settings
from app.core.database import Base
from app.tests.fakes import FakeGithubClient, FakeSupabaseClient
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
def github() -> FakeGithubClient:
    return FakeGithubClient()


@pytest.fixture
def supabase() -> FakeSupabaseClient:
    return FakeSupabaseClient()


@pytest.fixture
def client(settings: Settings, github: FakeGithubClient, supabase: FakeSupabaseClient) -> Iterator[TestClient]:
    app = create_app(settings)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_github_client] = lambda: github
    app.dependency_overrides[get_supabase_client] = lambda: supabase
    # The SPA's origin: in dev the Vite proxy serves the API under it.
    with TestClient(app, base_url=APP_URL, follow_redirects=False) as client:
        yield client
