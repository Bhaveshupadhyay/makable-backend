from uuid import UUID

from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class SiteAnalytics(TimestampMixin, Base):
    """The GA4 property and web stream a site reports to. Its deploy adds the stream's tag (`measurement_id`) to the
    site's pages, and its report reads that stream's numbers."""

    __tablename__ = "site_analytics"

    site_id: Mapped[UUID] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), primary_key=True)
    property_id: Mapped[str] = mapped_column(String(20))
    property_name: Mapped[str] = mapped_column(String(200))
    stream_id: Mapped[str] = mapped_column(String(20))
    stream_name: Mapped[str] = mapped_column(String(200))
    measurement_id: Mapped[str] = mapped_column(String(32))
    stream_url: Mapped[str | None] = mapped_column(String(500))
    # The property's time zone: its days are the report's days.
    time_zone: Mapped[str] = mapped_column(String(64))
    # The tag the site's last publish deployed (None: not published since it was linked).
    published_measurement_id: Mapped[str | None] = mapped_column(String(32))
