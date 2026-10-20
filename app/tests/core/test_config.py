import pytest
from pydantic import SecretStr

from app.core.config import Settings


@pytest.mark.parametrize(
    ("value", "expected"), [(None, None), ("", None), ("http://localhost:20128/v1/", "http://localhost:20128/v1")]
)
def test_an_empty_ai_base_url_means_no_makable_model(value: str | None, expected: str | None) -> None:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///x.db",
        secret_key=SecretStr("s" * 32),
        token_encryption_key=SecretStr("A" * 43 + "="),
        supabase_url="https://p.supabase.co",
        supabase_publishable_key="k",
        ai_base_url=value,
    )
    assert settings.ai_base_url == expected


@pytest.mark.parametrize(("client_id", "secret"), [(None, None), ("", ""), ("  ", "x"), ("id", "")])
def test_analytics_is_off_without_a_whole_google_client(client_id: str | None, secret: str | None) -> None:
    settings = Settings(
        _env_file=None,
        database_url="sqlite+aiosqlite:///x.db",
        secret_key=SecretStr("s" * 32),
        token_encryption_key=SecretStr("A" * 43 + "="),
        supabase_url="https://p.supabase.co",
        supabase_publishable_key="k",
        google_client_id=client_id,
        google_client_secret=SecretStr(secret) if secret is not None else None,
    )
    assert settings.google_client_id is None or settings.google_client_secret is None
    assert settings.google_callback_url == "http://localhost:5173/api/v1/analytics/google/callback"
