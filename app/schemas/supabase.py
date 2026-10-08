from uuid import UUID

from pydantic import BaseModel


class SupabaseUser(BaseModel):
    id: UUID


class SupabaseSession(BaseModel):
    """Supabase Auth's token response. `provider_token` is the user's GitHub token. Supabase only returns it
    from the sign-in exchange, never on refresh, and doesn't keep it."""

    access_token: str
    refresh_token: str
    expires_in: int
    user: SupabaseUser
    provider_token: str | None = None
