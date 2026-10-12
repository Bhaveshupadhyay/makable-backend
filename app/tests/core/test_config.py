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
