"""Startup and shutdown: opens the shared clients (`app/core/client.py`) and closes them."""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI

from app.core.client import close_connection, open_connection
from app.core.config import Settings

logger = logging.getLogger(__name__)


def build_lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # Schema changes go through Alembic (`alembic upgrade head`), never create_all here.
        await open_connection(settings)
        logger.info(
            "database pool created",
            extra={
                "database": settings.sqlalchemy_url.render_as_string(hide_password=True),
                "pool_size": None if settings.is_sqlite else settings.db_pool_size,
                "max_overflow": None if settings.is_sqlite else settings.db_max_overflow,
                "transaction_pooler": settings.db_transaction_pooler,
            },
        )
        try:
            yield
        finally:
            await close_connection()

    return lifespan
