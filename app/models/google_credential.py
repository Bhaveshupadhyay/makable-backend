from uuid import UUID

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class GoogleCredential(TimestampMixin, Base):
    """A user's Google connection for reading their Analytics: the refresh token (stored encrypted) that gets an
    access token for each request, and the account it belongs to."""

    __tablename__ = "google_credentials"

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    # Google's id for the account.
    google_sub: Mapped[str] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    refresh_token: Mapped[str] = mapped_column(Text)
    # The scopes the user granted, space-separated.
    scopes: Mapped[str] = mapped_column(Text)
