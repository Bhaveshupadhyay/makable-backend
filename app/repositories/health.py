from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession


class HealthRepository(Protocol):
    async def ping(self) -> None: ...


class SqlHealthRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def ping(self) -> None:
        """Raises if the database is unreachable."""
        await self._session.execute(text("SELECT 1"))
