from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.constants.ai_edit import AiEditOutcome
from app.core.database import Base, UTCDateTime, utc_now
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Site(TimestampMixin, Base):
    """A user's site: its repo on their GitHub (the code), and its AI chat (LangGraph's state, thread id = this
    id). This row says whose it is, because neither GitHub nor the checkpointer knows about makable's users."""

    __tablename__ = "sites"
    __table_args__ = (UniqueConstraint("user_id", "repo_name", name="uq_sites_user_id_repo_name"),)

    # The site's `projectId`, made by the SPA.
    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # What the user calls it.
    name: Mapped[str] = mapped_column(String(100))
    # The repo on the user's GitHub, once it's created (None until then).
    repo_name: Mapped[str | None] = mapped_column(String(100))


class AiEditRecord(UUIDPrimaryKeyMixin, Base):
    """One AI request and how it ended, as a plain row (the thread's state is opaque). Kept until the user starts
    the site over."""

    __tablename__ = "ai_edit_requests"

    site_id: Mapped[UUID] = mapped_column(ForeignKey("sites.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    instruction: Mapped[str] = mapped_column(Text)
    # The selected element as the SPA described it, or None for the whole page.
    selection: Mapped[dict[str, object] | None] = mapped_column(JSON)
    # The model that answered: a model id, makable's own model's name, or None when none was called.
    model: Mapped[str | None] = mapped_column(String(200))
    # 1 or 2; None when the request failed or was cancelled before an answer.
    tier: Mapped[int | None] = mapped_column(Integer)
    outcome: Mapped[AiEditOutcome] = mapped_column(
        Enum(AiEditOutcome, native_enum=False, length=16, values_callable=lambda members: [m.value for m in members])
    )
    # The change made, why it went to Tier 2, or what failed.
    summary: Mapped[str | None] = mapped_column(Text)
    # Model calls made.
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    # The commit the change made on the site's repo.
    commit_sha: Mapped[str | None] = mapped_column(String(40))
    # Rows are written once, when the request ends.
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
