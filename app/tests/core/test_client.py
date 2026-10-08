from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from app.core.client import (
    close_connection,
    close_postgres_client,
    engine_options,
    get_http_client,
    get_postgres_client,
    get_postgres_engine,
)
from app.core.config import DatabaseSettings


def db_settings(**values: Any) -> DatabaseSettings:
    # _env_file=None so the developer's real .env never leaks into tests.
    return DatabaseSettings(_env_file=None, **values)


def supabase(**values: Any) -> DatabaseSettings:
    return db_settings(
        db_host="aws-0-ap-northeast-1.pooler.supabase.com",
        db_user="postgres.ref",
        db_password=SecretStr("p@ss/w:rd%"),
        **values,
    )


def test_builds_a_postgres_url_and_escapes_the_password() -> None:
    url = supabase().sqlalchemy_url

    assert url.drivername == "postgresql+asyncpg"
    assert (url.host, url.port, url.database, url.username) == (
        "aws-0-ap-northeast-1.pooler.supabase.com",
        5432,
        "postgres",
        "postgres.ref",
    )
    assert url.password == "p@ss/w:rd%"
    assert "p@ss" not in url.render_as_string(hide_password=True)


def test_database_url_wins_and_gets_the_async_driver() -> None:
    url = supabase(database_url="postgres://u:p@db.example.com/app").sqlalchemy_url
    assert url.drivername == "postgresql+asyncpg"
    assert url.host == "db.example.com"


def test_requires_a_database() -> None:
    with pytest.raises(ValidationError, match="DATABASE_URL"):
        db_settings(db_host="h")


def test_postgres_gets_a_bounded_pre_pinged_pool() -> None:
    options = engine_options(supabase(db_pool_size=3, db_max_overflow=2))

    assert options["pool_size"] == 3
    assert options["max_overflow"] == 2
    assert options["pool_pre_ping"] is True
    assert options["connect_args"]["ssl"] == "require"
    assert "statement_cache_size" not in options["connect_args"]


def test_transaction_pooler_disables_the_statement_cache() -> None:
    connect_args = engine_options(supabase(db_transaction_pooler=True))["connect_args"]

    assert connect_args["statement_cache_size"] == 0
    name = connect_args["prepared_statement_name_func"]
    assert name() != name()


def test_sqlite_uses_default_options() -> None:
    assert engine_options(db_settings(database_url="sqlite+aiosqlite:///:memory:")) == {}


async def test_postgres_client_creates_a_pool_without_connecting() -> None:
    engine = get_postgres_engine(supabase(db_pool_size=4))
    assert engine.pool.size() == 4  # type: ignore[attr-defined]


async def test_client_is_a_singleton_until_closed() -> None:
    sqlite = db_settings(database_url="sqlite+aiosqlite:///:memory:")
    engine = get_postgres_engine(sqlite)

    assert get_postgres_engine() is engine
    assert get_postgres_client() is get_postgres_client()
    assert get_postgres_client().kw["bind"] is engine

    await close_postgres_client()
    assert get_postgres_engine(supabase()) is not engine


async def test_http_client_is_shared_and_closed_with_the_connection() -> None:
    client = get_http_client()
    assert get_http_client() is client

    await close_connection()

    assert client.is_closed
    assert get_http_client() is not client
