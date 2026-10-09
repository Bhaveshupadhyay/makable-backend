"""Application settings, read from environment variables and `.env`."""

from functools import lru_cache
from typing import Literal, Self

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL, make_url

from app.constants.api import API_V1_PREFIX

POSTGRES_DRIVER = "postgresql+asyncpg"


class DatabaseSettings(BaseSettings):
    """Just the database, for tools like Alembic that shouldn't need the app's secrets.

    Configure either the `DB_*` parts (Postgres) or a full `DATABASE_URL`, which wins if both are set.
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # A full SQLAlchemy URL, e.g. sqlite+aiosqlite:///./makable.db for offline dev. postgres:// works too.
    database_url: str | None = None

    db_host: str | None = None
    db_port: int = 5432
    db_name: str = "postgres"
    db_user: str | None = None
    db_password: SecretStr | None = None
    # "require" encrypts but doesn't verify the server's certificate, which works with Supabase out of the
    # box. In production use "verify-full" with Supabase's CA certificate (dashboard -> Database -> SSL) at
    # the path in PGSSLROOTCERT.
    db_ssl: Literal["disable", "prefer", "require", "verify-ca", "verify-full"] = "require"

    # Pool sizing is per process: total connections = workers x (pool_size + max_overflow). Keep it under
    # the pooler's client limit (Supabase's session mode caps it by plan).
    db_pool_size: int = Field(default=5, ge=1)
    db_max_overflow: int = Field(default=5, ge=0)
    # Seconds to wait for a free connection before failing the request.
    db_pool_timeout: float = Field(default=30, gt=0)
    # Seconds before a connection is replaced, so the pooler never hands us one it has already dropped.
    db_pool_recycle: int = Field(default=1800, gt=0)
    # Seconds before a single query is cancelled.
    db_command_timeout: float = Field(default=60, gt=0)
    # Transaction-mode poolers (Supabase port 6543, PgBouncer) can't keep prepared statements between
    # transactions, so asyncpg's statement cache must be off. Session mode (port 5432) doesn't need this.
    db_transaction_pooler: bool = False

    @model_validator(mode="after")
    def _database_configured(self) -> Self:
        if self.database_url is None and not (self.db_host and self.db_user and self.db_password):
            raise ValueError("set DATABASE_URL, or DB_HOST, DB_USER and DB_PASSWORD")
        return self

    @property
    def sqlalchemy_url(self) -> URL:
        """The URL with the async driver. Built with `URL.create`, so special characters in the password are safe."""
        if self.database_url is not None:
            url = make_url(self.database_url)
            if url.drivername in ("postgres", "postgresql"):
                url = url.set(drivername=POSTGRES_DRIVER)
            return url
        return URL.create(
            POSTGRES_DRIVER,
            username=self.db_user,
            password=self.db_password.get_secret_value() if self.db_password else None,
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
        )

    @property
    def is_sqlite(self) -> bool:
        return self.sqlalchemy_url.get_backend_name() == "sqlite"


class Settings(DatabaseSettings):
    """All configuration. Secrets have no defaults, so the app refuses to start without them."""

    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"

    # The SPA's origin. In dev the API is served under it through the Vite proxy, so cookies are same-origin.
    app_url: str = "http://localhost:5173"
    # Only needed if the SPA calls the API cross-origin. Empty means no CORS headers.
    cors_origins: list[str] = []

    # Signs the short-lived OAuth state cookie (PKCE verifier and return path).
    secret_key: SecretStr = Field(min_length=32)
    # Fernet key that encrypts GitHub tokens at rest.
    token_encryption_key: SecretStr

    # Supabase Auth runs the GitHub sign-in and issues the sessions. The URL is https://<ref>.supabase.co,
    # and the key is the project's publishable key (the legacy anon key works too). Neither is secret.
    supabase_url: str
    supabase_publishable_key: str
    # How long the browser keeps the refresh token cookie. Supabase decides when the token itself expires.
    refresh_token_ttl_days: int = 30

    # The model behind "Edit with AI": any OpenAI-compatible chat completions API. The default is a local
    # OmniRoute (`OMNIROUTE_SERVER_HOST=127.0.0.1 omniroute serve`), which needs no key.
    ai_base_url: str = "http://localhost:20128/v1"
    ai_api_key: SecretStr | None = None
    ai_model: str = "auto"
    ai_timeout_seconds: float = Field(default=90, gt=0)
    # Adds what the model was given to AI edit responses. Never turn it on in production.
    ai_debug: bool = False

    @field_validator("app_url", "supabase_url", "ai_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("token_encryption_key")
    @classmethod
    def _valid_fernet_key(cls, value: SecretStr) -> SecretStr:
        try:
            Fernet(value.get_secret_value())
        except ValueError as err:
            raise ValueError("must be a Fernet key (see .env.example)") from err
        return value

    @property
    def auth_callback_url(self) -> str:
        """Where Supabase sends the browser after sign-in. Add it to Supabase's allowed redirect URLs."""
        return f"{self.app_url}{API_V1_PREFIX}/auth/github/callback"

    @property
    def secure_cookies(self) -> bool:
        return self.app_url.startswith("https://")


@lru_cache
def get_settings() -> Settings:
    """The settings, read from the environment once. Tests swap them with `app.dependency_overrides`."""
    return Settings()
