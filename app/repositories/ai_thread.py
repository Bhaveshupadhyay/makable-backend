from typing import Protocol
from uuid import UUID

from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import utc_now
from app.core.exceptions import ConflictError
from app.models.ai_thread import AiEditRecord, AiThread
from app.schemas.ai_thread import AiEditRecordCreate, AiThreadCreate


class AiThreadRepository(Protocol):
    async def get(self, thread_id: UUID) -> AiThread | None: ...

    async def create(self, data: AiThreadCreate) -> AiThread: ...

    async def touch(self, thread_id: UUID) -> None: ...

    async def delete(self, thread: AiThread) -> None: ...


class AiEditRecordRepository(Protocol):
    async def add(self, data: AiEditRecordCreate) -> AiEditRecord: ...

    async def delete_for_thread(self, thread_id: UUID) -> None: ...


class SqlAiThreadRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, thread_id: UUID) -> AiThread | None:
        """The thread, whoever owns it (the service checks)."""
        return await self._session.get(AiThread, thread_id)

    async def create(self, data: AiThreadCreate) -> AiThread:
        """Adds a thread.

        Raises:
            ConflictError: A thread with this id was just added. The session must then be rolled back.
        """
        thread = AiThread(**data.model_dump())
        self._session.add(thread)
        try:
            await self._session.flush()
        except IntegrityError as err:
            raise ConflictError(
                "This site's AI thread was just created. Try again.", code="ai_thread_conflict"
            ) from err
        return thread

    async def touch(self, thread_id: UUID) -> None:
        """Marks the thread as used now."""
        await self._session.execute(update(AiThread).where(AiThread.id == thread_id).values(updated_at=utc_now()))

    async def delete(self, thread: AiThread) -> None:
        await self._session.delete(thread)
        await self._session.flush()


class SqlAiEditRecordRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, data: AiEditRecordCreate) -> AiEditRecord:
        record = AiEditRecord(**data.model_dump())
        self._session.add(record)
        await self._session.flush()
        return record

    async def delete_for_thread(self, thread_id: UUID) -> None:
        """Deletes the thread's requests. Done explicitly: SQLite doesn't enforce the foreign key's cascade."""
        await self._session.execute(delete(AiEditRecord).where(AiEditRecord.thread_id == thread_id))
