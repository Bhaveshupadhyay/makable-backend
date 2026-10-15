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
from langgraph.checkpoint.base import BaseCheckpointSaver
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.cookies import AuthCookies
from app.clients.ai_providers import AiProviderClient, HttpAiProviderClient
from app.clients.deploy_platforms import DeployPlatformClient, HttpDeployPlatformClient
from app.clients.github import GithubClient, HttpGithubClient
from app.clients.model import HttpModelClient, ModelClient
from app.clients.supabase import HttpSupabaseAuthClient, SupabaseAuthClient
from app.constants.ai_keys import KEY_CHECK_BURST, KEY_CHECK_EVERY_SECONDS
from app.constants.api import (
    MAX_CONCURRENT_AI_EDITS,
    MAX_CONCURRENT_CREATIONS,
    MAX_CONCURRENT_PUBLISH_REQUESTS,
    MAX_CONCURRENT_SITE_REQUESTS,
    PUBLISH_BURST,
    PUBLISH_EVERY_SECONDS,
    SITE_SAVE_BURST,
    SITE_SAVE_EVERY_SECONDS,
)
from app.constants.auth import ACCESS_TOKEN_COOKIE, REFRESH_TOKEN_COOKIE, Role
from app.core.client import get_checkpointer, get_db_session, get_http_client
from app.core.config import Settings, get_settings
from app.core.exceptions import ForbiddenError
from app.core.limits import ConcurrencyLimit, KeyLock, PerKeyLimit
from app.core.security import PayloadSigner, TokenCipher
from app.repositories.ai_key import AiKeyRepository, SqlAiKeyRepository
from app.repositories.github_credential import GithubCredentialRepository, SqlGithubCredentialRepository
from app.repositories.health import HealthRepository, SqlHealthRepository
from app.repositories.site import (
    AiEditRecordRepository,
    SiteRepository,
    SqlAiEditRecordRepository,
    SqlSiteRepository,
)
from app.repositories.unit_of_work import SqlAlchemyUnitOfWork, UnitOfWork
from app.repositories.user import SqlUserRepository, UserRepository
from app.schemas.ai_key import ModelEndpoint
from app.schemas.user import UserRead
from app.services.ai_edit_service import AiEditService, AiRunInProgressError
from app.services.ai_key_service import AiKeyService, ModelClientFactory
from app.services.auth_service import AuthService
from app.services.health_service import HealthService
from app.services.publish_service import PublishService
from app.services.site_create_service import SiteCreateService
from app.services.site_service import SiteService

# --- App-wide singletons ---


SettingsDep = Annotated[Settings, Depends(get_settings)]


DbSessionDep = Annotated[AsyncSession, Depends(get_db_session)]
HttpClientDep = Annotated[httpx.AsyncClient, Depends(get_http_client)]


def get_checkpoint_saver() -> BaseCheckpointSaver[str]:
    """LangGraph's checkpointer (Postgres, or in memory on SQLite), opened by the lifespan."""
    return get_checkpointer()


CheckpointerDep = Annotated[BaseCheckpointSaver[str], Depends(get_checkpoint_saver)]

# --- Infrastructure ---


def get_unit_of_work(session: DbSessionDep) -> UnitOfWork:
    return SqlAlchemyUnitOfWork(session)


def get_token_cipher(settings: SettingsDep) -> TokenCipher:
    return TokenCipher(settings.token_encryption_key.get_secret_value())


def get_oauth_state_signer(settings: SettingsDep) -> PayloadSigner:
    return PayloadSigner(settings.secret_key.get_secret_value(), salt="oauth-state")


def get_github_client(http: HttpClientDep) -> GithubClient:
    return HttpGithubClient(http)


def get_model_client(http: HttpClientDep, settings: SettingsDep) -> ModelClient | None:
    """makable's own model, for users who haven't picked one of theirs. None when `AI_BASE_URL` isn't set."""
    if settings.ai_base_url is None:
        return None
    endpoint = ModelEndpoint(
        base_url=settings.ai_base_url,
        api_key=settings.ai_api_key.get_secret_value() if settings.ai_api_key else None,
        model=settings.ai_model,
        accepts_images=settings.ai_accepts_images,
    )
    return HttpModelClient(http, endpoint, timeout_seconds=settings.ai_timeout_seconds)


def get_model_client_factory(http: HttpClientDep, settings: SettingsDep) -> ModelClientFactory:
    """Builds the client for a model the user picked, with their key."""
    return lambda endpoint: HttpModelClient(http, endpoint, timeout_seconds=settings.ai_timeout_seconds)


def get_ai_provider_client(http: HttpClientDep) -> AiProviderClient:
    return HttpAiProviderClient(http)


def get_deploy_platform_client(http: HttpClientDep) -> DeployPlatformClient:
    return HttpDeployPlatformClient(http)


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


def get_ai_key_repository(session: DbSessionDep) -> AiKeyRepository:
    return SqlAiKeyRepository(session)


def get_site_repository(session: DbSessionDep) -> SiteRepository:
    return SqlSiteRepository(session)


def get_ai_edit_record_repository(session: DbSessionDep) -> AiEditRecordRepository:
    return SqlAiEditRecordRepository(session)


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


def get_ai_key_service(
    keys: Annotated[AiKeyRepository, Depends(get_ai_key_repository)],
    providers: Annotated[AiProviderClient, Depends(get_ai_provider_client)],
    cipher: Annotated[TokenCipher, Depends(get_token_cipher)],
    uow: UnitOfWorkDep,
    default_model: Annotated[ModelClient | None, Depends(get_model_client)],
    model_client: Annotated[ModelClientFactory, Depends(get_model_client_factory)],
) -> AiKeyService:
    return AiKeyService(keys, providers, cipher, uow, default_model=default_model, model_client=model_client)


def get_site_service(
    github: Annotated[GithubClient, Depends(get_github_client)],
    sites: Annotated[SiteRepository, Depends(get_site_repository)],
    credentials: Annotated[GithubCredentialRepository, Depends(get_github_credential_repository)],
    cipher: Annotated[TokenCipher, Depends(get_token_cipher)],
    uow: UnitOfWorkDep,
) -> SiteService:
    return SiteService(github, sites, credentials, cipher, uow)


def get_ai_edit_service(
    models: Annotated[AiKeyService, Depends(get_ai_key_service)],
    site_service: Annotated[SiteService, Depends(get_site_service)],
    sites: Annotated[SiteRepository, Depends(get_site_repository)],
    records: Annotated[AiEditRecordRepository, Depends(get_ai_edit_record_repository)],
    uow: UnitOfWorkDep,
    checkpointer: CheckpointerDep,
    settings: SettingsDep,
) -> AiEditService:
    return AiEditService(models, site_service, sites, records, uow, checkpointer, AI_RUNS, debug=settings.ai_debug)


def get_publish_service(
    github: Annotated[GithubClient, Depends(get_github_client)],
    sites: Annotated[SiteService, Depends(get_site_service)],
    platforms: Annotated[DeployPlatformClient, Depends(get_deploy_platform_client)],
) -> PublishService:
    return PublishService(github, sites, platforms)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
PublishServiceDep = Annotated[PublishService, Depends(get_publish_service)]


def get_site_create_service(
    models: Annotated[AiKeyService, Depends(get_ai_key_service)],
    site_service: Annotated[SiteService, Depends(get_site_service)],
    sites: Annotated[SiteRepository, Depends(get_site_repository)],
    records: Annotated[AiEditRecordRepository, Depends(get_ai_edit_record_repository)],
    uow: UnitOfWorkDep,
    checkpointer: CheckpointerDep,
) -> SiteCreateService:
    return SiteCreateService(models, site_service, sites, records, uow, checkpointer, AI_RUNS)


AiEditServiceDep = Annotated[AiEditService, Depends(get_ai_edit_service)]
SiteCreateServiceDep = Annotated[SiteCreateService, Depends(get_site_create_service)]
SiteServiceDep = Annotated[SiteService, Depends(get_site_service)]
AiKeyServiceDep = Annotated[AiKeyService, Depends(get_ai_key_service)]
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
AI_CREATE_SLOTS = ConcurrencyLimit(MAX_CONCURRENT_CREATIONS)
PUBLISH_SLOTS = ConcurrencyLimit(MAX_CONCURRENT_PUBLISH_REQUESTS)
PUBLISHES = PerKeyLimit(burst=PUBLISH_BURST, every=PUBLISH_EVERY_SECONDS)
AI_KEY_CHECKS = PerKeyLimit(burst=KEY_CHECK_BURST, every=KEY_CHECK_EVERY_SECONDS)
# One AI request at a time per site (and per user), including Start over. The service holds it.
AI_RUNS = KeyLock(AiRunInProgressError)
SITE_SLOTS = ConcurrencyLimit(MAX_CONCURRENT_SITE_REQUESTS)
SITE_SAVES = PerKeyLimit(burst=SITE_SAVE_BURST, every=SITE_SAVE_EVERY_SECONDS)


async def ai_edit_slot() -> AsyncIterator[None]:
    """A slot for an AI edit, held until the response is done.

    Raises:
        ServiceBusyError: This worker has as many AI edits in progress as it allows.
    """
    async with AI_EDIT_SLOTS.slot():
        yield


async def ai_create_slot() -> AsyncIterator[None]:
    """A slot for a creation, held while it streams (minutes).

    Raises:
        ServiceBusyError: This worker has as many creations in progress as it allows.
    """
    async with AI_CREATE_SLOTS.slot():
        yield


async def publish_slot() -> AsyncIterator[None]:
    """A slot for a publish request (it waits on GitHub and the hosting platform), held until the response is done.

    Raises:
        ServiceBusyError: This worker has as many in progress as it allows.
    """
    async with PUBLISH_SLOTS.slot():
        yield


async def publish_limit(user: CurrentUserDep) -> AsyncIterator[None]:
    """One publish or deploy at a time per user, in bursts of at most `PUBLISH_BURST`, then one per
    `PUBLISH_EVERY_SECONDS`.

    Raises:
        TooManyRequestsError: The user has one in progress, or published too often.
    """
    async with PUBLISHES.hold(user.id):
        yield


async def ai_key_check_limit(user: CurrentUserDep) -> AsyncIterator[None]:
    """One key check at a time per user, in bursts of at most `KEY_CHECK_BURST`, then one per
    `KEY_CHECK_EVERY_SECONDS`. A check calls the provider with the key, so this also keeps the endpoint from being
    used to test lists of stolen keys.

    Raises:
        TooManyRequestsError: The user has one in progress, or checked too often.
    """
    async with AI_KEY_CHECKS.hold(user.id):
        yield


async def site_slot() -> AsyncIterator[None]:
    """A slot for a site request (it waits on GitHub), held until the response is done.

    Raises:
        ServiceBusyError: This worker has as many in progress as it allows.
    """
    async with SITE_SLOTS.slot():
        yield


async def site_save_limit(user: CurrentUserDep) -> AsyncIterator[None]:
    """One save of manual edits at a time per user, in bursts of at most `SITE_SAVE_BURST`, then one per
    `SITE_SAVE_EVERY_SECONDS`.

    Raises:
        TooManyRequestsError: The user has a save in progress, or saved too often.
    """
    async with SITE_SAVES.hold(user.id):
        yield
