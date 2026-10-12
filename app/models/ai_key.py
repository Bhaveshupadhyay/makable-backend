from datetime import datetime
from uuid import UUID

from sqlalchemy import JSON, Enum, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.constants.ai_keys import AiKeyStatus, AiProvider
from app.core.database import Base, UTCDateTime
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class AiKey(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """A user's own API key for an AI provider, one per provider. Stored encrypted."""

    __tablename__ = "ai_keys"
    __table_args__ = (UniqueConstraint("user_id", "provider"),)

    # The unique (user_id, provider) constraint also indexes user_id.
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    provider: Mapped[AiProvider] = mapped_column(
        Enum(AiProvider, native_enum=False, length=32, values_callable=lambda members: [m.value for m in members])
    )
    api_key: Mapped[str] = mapped_column(Text)
    key_hint: Mapped[str] = mapped_column(String(8))
    status: Mapped[AiKeyStatus] = mapped_column(
        Enum(AiKeyStatus, native_enum=False, length=16, values_callable=lambda members: [m.value for m in members])
    )
    # When the provider last accepted or refused the key.
    checked_at: Mapped[datetime] = mapped_column(UTCDateTime)
    # The models the user shows in the chat's menu. Always assigned a new list, so the change is seen.
    models: Mapped[list[str]] = mapped_column(JSON, default=list)
