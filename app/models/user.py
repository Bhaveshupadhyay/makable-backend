from uuid import UUID

from sqlalchemy import BigInteger, Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from app.constants.auth import Role
from app.core.database import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    # Supabase Auth's user id (`auth.users.id`, the access token's `sub`).
    auth_user_id: Mapped[UUID] = mapped_column(unique=True, index=True)
    # Users are matched by GitHub id, not login: logins can be renamed.
    github_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    # Profile fields are a cache of GitHub's, refreshed on every login.
    login: Mapped[str] = mapped_column(String(39))
    name: Mapped[str | None] = mapped_column(String(255))
    avatar_url: Mapped[str] = mapped_column(String(2048))
    role: Mapped[Role] = mapped_column(
        Enum(Role, native_enum=False, length=16, values_callable=lambda roles: [r.value for r in roles]),
        default=Role.USER,
    )
