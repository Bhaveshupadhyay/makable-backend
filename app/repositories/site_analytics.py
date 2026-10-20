from typing import Protocol
from uuid import UUID

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import utc_now
from app.models.site_analytics import SiteAnalytics
from app.schemas.analytics import SiteAnalyticsUpsert


class SiteAnalyticsRepository(Protocol):
    async def get(self, site_id: UUID) -> SiteAnalytics | None: ...

    async def upsert(self, data: SiteAnalyticsUpsert) -> SiteAnalytics: ...

    async def set_published(self, site_id: UUID, measurement_id: str | None) -> None: ...

    async def delete_for_site(self, site_id: UUID) -> None: ...


class SqlSiteAnalyticsRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, site_id: UUID) -> SiteAnalytics | None:
        """The site's link, whoever owns the site (the service checks)."""
        return await self._session.get(SiteAnalytics, site_id)

    async def upsert(self, data: SiteAnalyticsUpsert) -> SiteAnalytics:
        """Links the site to a stream, or replaces its link. What was published stays as it was."""
        link = await self.get(data.site_id)
        if link is None:
            link = SiteAnalytics(**data.model_dump())
            self._session.add(link)
        else:
            for field, value in data.model_dump(exclude={"site_id"}).items():
                setattr(link, field, value)
        await self._session.flush()
        return link

    async def set_published(self, site_id: UUID, measurement_id: str | None) -> None:
        """Records the tag the site's deploy now has. Nothing for a site without a link."""
        await self._session.execute(
            update(SiteAnalytics)
            .where(SiteAnalytics.site_id == site_id)
            .values(published_measurement_id=measurement_id, updated_at=utc_now())
        )

    async def delete_for_site(self, site_id: UUID) -> None:
        """Done explicitly: SQLite doesn't enforce the foreign key's cascade."""
        await self._session.execute(delete(SiteAnalytics).where(SiteAnalytics.site_id == site_id))
