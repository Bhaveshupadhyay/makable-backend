from enum import StrEnum

from app.constants.api import API_V1_PREFIX


class Role(StrEnum):
    USER = "user"
    ADMIN = "admin"


ACCESS_TOKEN_COOKIE = "makable_access"
REFRESH_TOKEN_COOKIE = "makable_refresh"
OAUTH_STATE_COOKIE = "makable_oauth_state"

# Each cookie is only sent where it's needed: the refresh token never reaches ordinary endpoints.
ACCESS_TOKEN_COOKIE_PATH = API_V1_PREFIX
REFRESH_TOKEN_COOKIE_PATH = f"{API_V1_PREFIX}/auth"
OAUTH_STATE_COOKIE_PATH = f"{API_V1_PREFIX}/auth/github"

OAUTH_STATE_TTL_SECONDS = 10 * 60

# Supabase signs users in with GitHub, asking for these OAuth App scopes (on top of Supabase's own
# `user:email`). `repo` lets us create repositories and push commits for the user.
AUTH_PROVIDER = "github"
GITHUB_SCOPES = "repo"
