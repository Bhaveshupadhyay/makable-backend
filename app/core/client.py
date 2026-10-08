"""Shared clients, one per process, created lazily: Postgres (engine + session factory) and HTTP.

`open_connection` runs at startup (lifespan) and `close_connection` at shutdown. Everything else gets
sessions through `get_db_session` (or the factory from `get_postgres_client`) and HTTP through `get_http_client`.
"""

from collections.abc import AsyncIterator
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import DatabaseSettings

# Shows up in pg_stat_activity, so our connections are easy to spot.
APPLICATION_NAME = "makable-backend"
HTTP_TIMEOUT_SECONDS = 10

_postgres_engine: AsyncEngine | None = None
_postgres_sessionmaker: async_sessionmaker[AsyncSession] | None = None
_http_client: httpx.AsyncClient | None = None


def engine_options(settings: DatabaseSettings) -> dict[str, Any]:
    """`create_async_engine` options: a bounded, self-healing pool for Postgres, defaults for SQLite."""
    if settings.is_sqlite:
        return {}
    connect_args: dict[str, Any] = {
        "ssl": settings.db_ssl,
        "command_timeout": settings.db_command_timeout,
        "server_settings": {"application_name": APPLICATION_NAME},
    }
    if settings.db_transaction_pooler:
        # A transaction-mode pooler may run each transaction on a different server connection, where
        # our cached prepared statements don't exist. Unique names stop clashes with other clients'.
        connect_args["statement_cache_size"] = 0
        connect_args["prepared_statement_name_func"] = lambda: f"__asyncpg_{uuid4()}__"
    return {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout,
        "pool_recycle": settings.db_pool_recycle,
        # Checks each connection on checkout, so one the pooler closed is replaced instead of failing a request.
        "pool_pre_ping": True,
        "connect_args": connect_args,
    }


def get_postgres_engine(settings: DatabaseSettings | None = None) -> AsyncEngine:
    """The engine, created on first call. `settings` only applies then; it defaults to the environment."""
    global _postgres_engine
    if _postgres_engine is None:
        settings = settings or DatabaseSettings()
        _postgres_engine = create_async_engine(settings.sqlalchemy_url, echo=False, **engine_options(settings))
    return _postgres_engine


def get_postgres_client(settings: DatabaseSettings | None = None) -> async_sessionmaker[AsyncSession]:
    """The session factory, bound to the engine."""
    global _postgres_sessionmaker
    if _postgres_sessionmaker is None:
        _postgres_sessionmaker = async_sessionmaker(
            bind=get_postgres_engine(settings),
            class_=AsyncSession,
            autoflush=False,
            expire_on_commit=False,
        )
    return _postgres_sessionmaker


def get_http_client() -> httpx.AsyncClient:
    """One HTTP client for outbound calls (GitHub), so its connections are pooled and reused."""
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS)
    return _http_client


async def open_connection(settings: DatabaseSettings | None = None) -> None:
    """Creates every client at startup, so the first request doesn't pay for it."""
    get_postgres_client(settings)
    get_http_client()


async def close_postgres_client() -> None:
    """Closes every pooled connection. The next `get_postgres_*` call starts a fresh pool."""
    global _postgres_engine, _postgres_sessionmaker
    if _postgres_engine is not None:
        await _postgres_engine.dispose()
    _postgres_engine = None
    _postgres_sessionmaker = None


async def close_http_client() -> None:
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
    _http_client = None


async def close_connection() -> None:
    await close_http_client()
    await close_postgres_client()


async def get_db_session() -> AsyncIterator[AsyncSession]:
    """Yields a session that borrows a pooled connection. Services commit; anything uncommitted is
    rolled back and the connection returned to the pool on close."""
    async with get_postgres_client()() as session:
        yield session
