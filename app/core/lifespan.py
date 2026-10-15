"""Startup and shutdown: opens the shared clients (`app/core/client.py`) and closes them."""

import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager

from fastapi import FastAPI

from app.core.client import close_connection, open_connection
from app.core.config import Settings

logger = logging.getLogger(__name__)

# The variables langsmith reads to turn tracing on (`langsmith.utils.tracing_is_enabled`).
TRACING_VARIABLES = ("LANGSMITH_TRACING_V2", "LANGCHAIN_TRACING_V2", "LANGSMITH_TRACING", "LANGCHAIN_TRACING")


def refuse_tracing() -> None:
    """LangGraph would send every run (users' prompts and code) to LangSmith if tracing were turned on through
    `LANGSMITH_*` / `LANGCHAIN_*` variables. Nothing may leave the server, so the app won't start with it.

    Raises:
        RuntimeError: Tracing is turned on.
    """
    if any(os.environ.get(name, "").strip().lower() == "true" for name in TRACING_VARIABLES):
        raise RuntimeError("LangSmith tracing is on (LANGSMITH_TRACING / LANGCHAIN_TRACING_V2); turn it off")


def build_lifespan(settings: Settings) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        refuse_tracing()
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
