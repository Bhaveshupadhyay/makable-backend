from typing import Protocol
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import utc_now
from app.core.exceptions import ConflictError
from app.models.site import AiEditRecord, Site
from app.schemas.site import AiEditRecordCreate, SiteCreate


class SiteRepository(Protocol):
    async def get(self, site_id: UUID) -> Site | None: ...

    async def list_for_user(self, user_id: UUID) -> list[Site]: ...

    async def create(self, data: SiteCreate) -> Site: ...

    async def set_repo(self, site_id: UUID, repo_name: str) -> None: ...

    async def touch(self, site_id: UUID) -> None: ...

    async def delete(self, site: Site) -> None: ...


class AiEditRecordRepository(Protocol):
    async def add(self, data: AiEditRecordCreate) -> AiEditRecord: ...

    async def delete_for_site(self, site_id: UUID) -> None: ...


class SqlSiteRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, site_id: UUID) -> Site | None:
        """The site, whoever owns it (the service checks)."""
        return await self._session.get(Site, site_id)

    async def list_for_user(self, user_id: UUID) -> list[Site]:
        """The user's sites, the most recently used first."""
        rows = await self._session.scalars(
            select(Site).where(Site.user_id == user_id).order_by(Site.updated_at.desc(), Site.id)
        )
        return list(rows)

    async def create(self, data: SiteCreate) -> Site:
        """Adds a site.

        Raises:
            ConflictError: A site with this id (or this repo) was just added. The session must then be rolled back.
        """
        site = Site(**data.model_dump())
        self._session.add(site)
        try:
            await self._session.flush()
        except IntegrityError as err:
            raise ConflictError("This site was just created. Try again.", code="site_conflict") from err
        return site

    async def set_repo(self, site_id: UUID, repo_name: str) -> None:
        await self._session.execute(
            update(Site).where(Site.id == site_id).values(repo_name=repo_name, updated_at=utc_now())
        )

    async def touch(self, site_id: UUID) -> None:
        """Marks the site as used now."""
        await self._session.execute(update(Site).where(Site.id == site_id).values(updated_at=utc_now()))

    async def delete(self, site: Site) -> None:
        await self._session.delete(site)
        await self._session.flush()


class SqlAiEditRecordRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, data: AiEditRecordCreate) -> AiEditRecord:
        record = AiEditRecord(**data.model_dump())
        self._session.add(record)
        await self._session.flush()
        return record

    async def delete_for_site(self, site_id: UUID) -> None:
        """Deletes the site's requests. Done explicitly: SQLite doesn't enforce the foreign key's cascade."""
        await self._session.execute(delete(AiEditRecord).where(AiEditRecord.site_id == site_id))
