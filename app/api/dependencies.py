"""Dependency injection wiring: the only place that knows which implementation backs each interface.

App-wide singletons are the shared clients (`app/core/client.py`, opened by the lifespan) and the
settings (`get_settings`). Everything else is built per request. Tests replace any provider with
`app.dependency_overrides`.
"""

from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from typing import Annotated

import httpx
from fastapi import Cookie, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.cookies import AuthCookies
from app.clients.github import GithubClient, HttpGithubClient
from app.clients.model import HttpModelClient, ModelClient
from app.clients.supabase import HttpSupabaseAuthClient, SupabaseAuthClient
from app.constants.api import (
    MAX_CONCURRENT_AI_EDITS,
    MAX_CONCURRENT_WORKSPACE_REQUESTS,
    WORKSPACE_SAVE_BURST,
    WORKSPACE_SAVE_EVERY_SECONDS,
)
from app.constants.auth import ACCESS_TOKEN_COOKIE, REFRESH_TOKEN_COOKIE, Role
from app.core.client import get_db_session, get_http_client
from app.core.config import Settings, get_settings
from app.core.exceptions import ForbiddenError
from app.core.limits import ConcurrencyLimit, PerKeyLimit
from app.core.security import PayloadSigner, TokenCipher
from app.repositories.github_credential import GithubCredentialRepository, SqlGithubCredentialRepository
from app.repositories.health import HealthRepository, SqlHealthRepository
from app.repositories.unit_of_work import SqlAlchemyUnitOfWork, UnitOfWork
from app.repositories.user import SqlUserRepository, UserRepository
from app.schemas.user import UserRead
from app.services.ai_edit_service import AiEditService
from app.services.auth_service import AuthService
from app.services.health_service import HealthService
from app.services.workspace_service import WorkspaceService

# --- App-wide singletons ---


SettingsDep = Annotated[Settings, Depends(get_settings)]


DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
HttpClientDep = Annotated[httpx.AsyncClient, Depends(get_http_client)]

# --- Infrastructure ---


def get_unit_of_work(session: DbSessionDep) -> UnitOfWork:
    return SqlAlchemyUnitOfWork(session)


def get_token_cipher(settings: SettingsDep) -> TokenCipher:
    return TokenCipher(settings.token_encryption_key.get_secret_value())


def get_oauth_state_signer(settings: SettingsDep) -> PayloadSigner:
    return PayloadSigner(settings.secret_key.get_secret_value(), salt="oauth-state")


def get_github_client(http: HttpClientDep) -> GithubClient:
    return HttpGithubClient(http)


def get_model_client(http: HttpClientDep, settings: SettingsDep) -> ModelClient:
    return HttpModelClient(
        http,
        base_url=settings.ai_base_url,
        api_key=settings.ai_api_key.get_secret_value() if settings.ai_api_key else None,
        model=settings.ai_model,
        timeout_seconds=settings.ai_timeout_seconds,
    )


def get_supabase_client(http: HttpClientDep, settings: SettingsDep) -> SupabaseAuthClient:
    return HttpSupabaseAuthClient(http, url=settings.supabase_url, api_key=settings.supabase_publishable_key)


def get_auth_cookies(settings: SettingsDep) -> AuthCookies:
    return AuthCookies(secure=settings.secure_cookies, refresh_ttl=timedelta(days=settings.refresh_token_ttl_days))


UnitOfWorkDep = Annotated[UnitOfWork, Depends(get_unit_of_work)]
AuthCookiesDep = Annotated[AuthCookies, Depends(get_auth_cookies)]

# --- Repositories ---


def get_user_repository(session: DbSessionDep) -> UserRepository:
    return SqlUserRepository(session)


def get_github_credential_repository(session: DbSessionDep) -> GithubCredentialRepository:
    return SqlGithubCredentialRepository(session)


def get_health_repository(session: DbSessionDep) -> HealthRepository:
    return SqlHealthRepository(session)


UserRepositoryDep = Annotated[UserRepository, Depends(get_user_repository)]

# --- Services ---


def get_auth_service(
    supabase: Annotated[SupabaseAuthClient, Depends(get_supabase_client)],
    github: Annotated[GithubClient, Depends(get_github_client)],
    state_signer: Annotated[PayloadSigner, Depends(get_oauth_state_signer)],
    users: UserRepositoryDep,
    credentials: Annotated[GithubCredentialRepository, Depends(get_github_credential_repository)],
    cipher: Annotated[TokenCipher, Depends(get_token_cipher)],
    uow: UnitOfWorkDep,
    settings: SettingsDep,
) -> AuthService:
    return AuthService(
        supabase=supabase,
        github=github,
        state_signer=state_signer,
        users=users,
        credentials=credentials,
        cipher=cipher,
        uow=uow,
        callback_url=settings.auth_callback_url,
    )


def get_health_service(health: Annotated[HealthRepository, Depends(get_health_repository)]) -> HealthService:
    return HealthService(health)


def get_ai_edit_service(
    model: Annotated[ModelClient, Depends(get_model_client)], settings: SettingsDep
) -> AiEditService:
    return AiEditService(model, debug=settings.ai_debug)


def get_workspace_service(
    github: Annotated[GithubClient, Depends(get_github_client)],
    credentials: Annotated[GithubCredentialRepository, Depends(get_github_credential_repository)],
    cipher: Annotated[TokenCipher, Depends(get_token_cipher)],
) -> WorkspaceService:
    return WorkspaceService(github, credentials, cipher)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
WorkspaceServiceDep = Annotated[WorkspaceService, Depends(get_workspace_service)]
AiEditServiceDep = Annotated[AiEditService, Depends(get_ai_edit_service)]
HealthServiceDep = Annotated[HealthService, Depends(get_health_service)]

# --- Authentication ---

AccessTokenDep = Annotated[str | None, Cookie(alias=ACCESS_TOKEN_COOKIE)]
RefreshTokenDep = Annotated[str | None, Cookie(alias=REFRESH_TOKEN_COOKIE)]


async def get_current_user(auth: AuthServiceDep, access_token: AccessTokenDep = None) -> UserRead:
    """The signed-in user, from the access token cookie.

    Raises:
        UnauthorizedError: No valid access token, or its user isn't known here.
    """
    return await auth.authenticate(access_token)


CurrentUserDep = Annotated[UserRead, Depends(get_current_user)]


def require_roles(*roles: Role) -> Callable[[UserRead], Awaitable[UserRead]]:
    """A dependency that only lets users with one of `roles` through, e.g.
    `Depends(require_roles(Role.ADMIN))`."""

    async def check(user: CurrentUserDep) -> UserRead:
        if user.role not in roles:
            raise ForbiddenError()
        return user

    return check


# --- Limits (per worker; see `core/limits.py`) ---

AI_EDIT_SLOTS = ConcurrencyLimit(MAX_CONCURRENT_AI_EDITS)
WORKSPACE_SLOTS = ConcurrencyLimit(MAX_CONCURRENT_WORKSPACE_REQUESTS)
WORKSPACE_SAVES = PerKeyLimit(burst=WORKSPACE_SAVE_BURST, every=WORKSPACE_SAVE_EVERY_SECONDS)


async def ai_edit_slot() -> AsyncIterator[None]:
    """A slot for an AI edit, held until the response is done.

    Raises:
        ServiceBusyError: This worker has as many AI edits in progress as it allows.
    """
    async with AI_EDIT_SLOTS.slot():
        yield


async def workspace_slot() -> AsyncIterator[None]:
    """A slot for a workspace request (it waits on GitHub), held until the response is done.

    Raises:
        ServiceBusyError: This worker has as many in progress as it allows.
    """
    async with WORKSPACE_SLOTS.slot():
        yield


async def workspace_save_limit(user: CurrentUserDep) -> AsyncIterator[None]:
    """One save at a time per user, in bursts of at most `WORKSPACE_SAVE_BURST`, then one per
    `WORKSPACE_SAVE_EVERY_SECONDS`. The SPA already saves far less often; this is for clients that don't.

    Raises:
        TooManyRequestsError: The user has a save in progress, or saved too often.
    """
    async with WORKSPACE_SAVES.hold(user.id):
        yield
