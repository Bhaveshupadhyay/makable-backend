from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.constants.ai_edit import AiEditOutcome
from app.core.database import Base, UTCDateTime, utc_now
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class AiThread(TimestampMixin, Base):
    """A site's AI conversation. The conversation itself is LangGraph's state (thread id = this id); this row says
    whose it is, because the checkpointer has no access control."""

    __tablename__ = "ai_threads"

    # The site's `projectId`, made by the SPA.
    id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # The site's repo on GitHub, once it has one.
    repo_name: Mapped[str | None] = mapped_column(String(100))


class AiEditRecord(UUIDPrimaryKeyMixin, Base):
    """One AI edit request and how it ended, as a plain row (the thread's state is opaque). Kept until the user
    starts the site over."""

    __tablename__ = "ai_edit_requests"

    thread_id: Mapped[UUID] = mapped_column(ForeignKey("ai_threads.id", ondelete="CASCADE"), index=True)
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
    # The commit the change made on the site's repo (from step 2).
    commit_sha: Mapped[str | None] = mapped_column(String(40))
    # Rows are written once, when the request ends.
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now)
