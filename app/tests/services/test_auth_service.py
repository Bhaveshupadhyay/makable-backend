import pytest
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import LoginError, UnauthorizedError
from app.core.security import PayloadSigner, TokenCipher, pkce_challenge
from app.repositories.github_credential import SqlGithubCredentialRepository
from app.repositories.unit_of_work import SqlAlchemyUnitOfWork
from app.repositories.user import SqlUserRepository
from app.services.auth_service import AuthService
from app.tests.fakes import GITHUB_TOKEN, VALID_CODE, FakeGithubClient, FakeSupabaseClient

CIPHER = TokenCipher(Fernet.generate_key().decode())


@pytest.fixture
def service(session: AsyncSession, supabase: FakeSupabaseClient, github: FakeGithubClient) -> AuthService:
    return AuthService(
        supabase=supabase,
        github=github,
        state_signer=PayloadSigner("secret", salt="test"),
        users=SqlUserRepository(session),
        credentials=SqlGithubCredentialRepository(session),
        cipher=CIPHER,
        uow=SqlAlchemyUnitOfWork(session),
        callback_url="http://localhost:5173/api/v1/auth/github/callback",
    )


async def test_login_creates_the_user_and_stores_the_encrypted_github_token(
    service: AuthService, session: AsyncSession, supabase: FakeSupabaseClient
) -> None:
    login = service.start_login("/projects")

    result = await service.complete_login(state_cookie=login.state_cookie, code=VALID_CODE, error=None)

    assert result.return_to == "/projects"
    user = await SqlUserRepository(session).get_by_auth_user_id(supabase.auth_user_id)
    assert user is not None and user.login == "octocat"
    credential = await SqlGithubCredentialRepository(session).get(user.id)
    assert credential is not None
    assert credential.access_token != GITHUB_TOKEN
    assert CIPHER.decrypt(credential.access_token) == GITHUB_TOKEN
    assert (await service.authenticate(result.tokens.access_token)).id == user.id


async def test_start_login_sanitizes_return_to(service: AuthService) -> None:
    login = service.start_login("//evil.com")

    result = await service.complete_login(state_cookie=login.state_cookie, code=VALID_CODE, error=None)
    assert result.return_to == "/"


async def test_tampered_state_cookie_is_rejected_without_trusting_return_to(service: AuthService) -> None:
    login = service.start_login("/projects")

    with pytest.raises(LoginError) as exc:
        await service.complete_login(state_cookie=login.state_cookie + "x", code=VALID_CODE, error=None)
    assert (exc.value.code, exc.value.return_to) == ("invalid_state", "/")


async def test_a_code_from_another_login_is_rejected(service: AuthService) -> None:
    """Login CSRF: the code must be redeemed with this browser's PKCE verifier."""
    mine = service.start_login("/projects")
    service.start_login("/")  # someone else's login; the fake now expects their challenge

    with pytest.raises(LoginError) as exc:
        await service.complete_login(state_cookie=mine.state_cookie, code=VALID_CODE, error=None)
    assert (exc.value.code, exc.value.return_to) == ("auth_error", "/projects")


async def test_authenticate_rejects_unknown_tokens_and_users(
    service: AuthService, supabase: FakeSupabaseClient
) -> None:
    with pytest.raises(UnauthorizedError):
        await service.authenticate(None)
    with pytest.raises(UnauthorizedError):
        await service.authenticate("not-a-token")

    # A valid Supabase session for someone who never completed sign-in here.
    supabase.authorize_url(provider="github", scopes="repo", redirect_to="/", code_challenge=pkce_challenge("v"))
    session = await supabase.exchange_code(auth_code=VALID_CODE, code_verifier="v")
    with pytest.raises(UnauthorizedError):
        await service.authenticate(session.access_token)
