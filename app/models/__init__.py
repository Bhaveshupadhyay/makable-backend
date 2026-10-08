"""Every ORM model. Importing this package registers all tables on `Base.metadata` (Alembic relies on it)."""

from app.models.github_credential import GithubCredential
from app.models.user import User

__all__ = ["GithubCredential", "User"]
