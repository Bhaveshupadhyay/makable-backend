from uuid import UUID

from sqlalchemy import ForeignKey, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class GithubCredential(TimestampMixin, Base):
    """A user's GitHub token from signing in through Supabase, kept for acting on their repos. It's an
    OAuth App token, so it never expires and has no refresh token. Stored encrypted."""

    __tablename__ = "github_credentials"

    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    access_token: Mapped[str] = mapped_column(Text)
