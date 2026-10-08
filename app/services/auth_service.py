import logging

from pydantic import ValidationError

from app.clients.github import GithubClient, GithubError
from app.clients.supabase import SupabaseAuthClient, SupabaseError, SupabaseRejectedError
from app.constants.auth import AUTH_PROVIDER, GITHUB_SCOPES, OAUTH_STATE_TTL_SECONDS
from app.core.exceptions import ConflictError, LoginError, UnauthorizedError
from app.core.security import InvalidTokenError, PayloadSigner, TokenCipher, generate_token, pkce_challenge
from app.repositories.github_credential import GithubCredentialRepository
from app.repositories.unit_of_work import UnitOfWork
from app.repositories.user import UserRepository
from app.schemas.auth import LoginRedirect, LoginResult, OAuthState, SessionTokens
from app.schemas.github import GithubCredentialUpsert, GithubUser
from app.schemas.supabase import SupabaseSession
from app.schemas.user import UserCreate, UserRead, UserUpdate
from app.utils.return_to import safe_return_to

logger = logging.getLogger(__name__)


def _session_tokens(session: SupabaseSession) -> SessionTokens:
    return SessionTokens(
        access_token=session.access_token, refresh_token=session.refresh_token, expires_in=session.expires_in
    )


class AuthService:
    """Sign-in with GitHub through Supabase Auth, and the session that follows.

    Supabase runs the OAuth flow with GitHub and issues the session. We keep its tokens in HttpOnly
    cookies and verify its access tokens. We also store the user's GitHub token, because Supabase
    hands it over only once, right after sign-in.
    """

    def __init__(
        self,
        *,
        supabase: SupabaseAuthClient,
        github: GithubClient,
        state_signer: PayloadSigner,
        users: UserRepository,
        credentials: GithubCredentialRepository,
        cipher: TokenCipher,
        uow: UnitOfWork,
        callback_url: str,
    ) -> None:
        self._supabase = supabase
        self._github = github
        self._state_signer = state_signer
        self._users = users
        self._credentials = credentials
        self._cipher = cipher
        self._uow = uow
        self._callback_url = callback_url

    def start_login(self, return_to: str | None) -> LoginRedirect:
        """Builds the Supabase authorize URL and the signed state the callback will check.

        There's no separate `state` parameter: Supabase only redeems the code with the PKCE verifier in
        this browser's cookie, so a code from someone else's sign-in (login CSRF) fails.

        Args:
            return_to: Where to send the browser afterwards. Unsafe values become `/`.
        """
        oauth = OAuthState(code_verifier=generate_token(48), return_to=safe_return_to(return_to))
        return LoginRedirect(
            authorize_url=self._supabase.authorize_url(
                provider=AUTH_PROVIDER,
                scopes=GITHUB_SCOPES,
                redirect_to=self._callback_url,
                code_challenge=pkce_challenge(oauth.code_verifier),
            ),
            state_cookie=self._state_signer.dumps(oauth.model_dump()),
        )

    async def complete_login(
        self, *, state_cookie: str | None, code: str | None, error: str | None, error_description: str | None = None
    ) -> LoginResult:
        """Handles the redirect back from Supabase: redeems the code, signs the user in and stores their
        GitHub token.

        Raises:
            LoginError: The state cookie is missing, forged or expired, the user declined, Supabase or
                GitHub failed, or a concurrent sign-in created the same user. `return_to` is only trusted
                (and set) once the cookie has been verified.
        """
        oauth = self._verify_state(state_cookie)
        if error:
            raise LoginError(error, error_description or "Sign-in returned an error", return_to=oauth.return_to)
        if not code:
            raise LoginError("missing_code", return_to=oauth.return_to)

        try:
            session = await self._supabase.exchange_code(auth_code=code, code_verifier=oauth.code_verifier)
        except SupabaseError as err:
            raise LoginError("auth_error", err.message, return_to=oauth.return_to) from err
        if not session.provider_token:
            raise LoginError("github_error", "Supabase returned no GitHub token", return_to=oauth.return_to)
        try:
            github_user = await self._github.get_user(session.provider_token)
        except GithubError as err:
            raise LoginError("github_error", err.message, return_to=oauth.return_to) from err

        try:
            user = await self._upsert_user(session, github_user)
        except ConflictError as err:
            # A concurrent sign-in for the same GitHub account created the user first.
            await self._uow.rollback()
            raise LoginError("conflict", "Another sign-in created this user first", return_to=oauth.return_to) from err
        await self._save_credentials(user, session.provider_token)
        await self._uow.commit()
        logger.info("user signed in", extra={"user_id": str(user.id)})
        return LoginResult(tokens=_session_tokens(session), return_to=oauth.return_to)

    async def authenticate(self, access_token: str | None) -> UserRead:
        """The user an access token belongs to.

        Raises:
            UnauthorizedError: No token, an invalid or expired one, or its user isn't known here.
        """
        if not access_token:
            raise UnauthorizedError()
        try:
            claims = await self._supabase.verify(access_token)
        except InvalidTokenError as err:
            raise UnauthorizedError() from err
        user = await self._users.get_by_auth_user_id(claims.sub)
        if user is None:
            raise UnauthorizedError()
        return UserRead.model_validate(user)

    async def refresh(self, refresh_token: str) -> SessionTokens:
        """Exchanges a refresh token for a new session. The old refresh token stops working.

        Raises:
            UnauthorizedError: The refresh token is unknown, already used or revoked.
        """
        try:
            session = await self._supabase.refresh(refresh_token)
        except SupabaseRejectedError as err:
            raise UnauthorizedError("Session expired") from err
        return _session_tokens(session)

    async def logout(self, *, access_token: str | None, refresh_token: str | None) -> None:
        """Ends the Supabase session so its refresh token stops working. Best effort: failures are
        logged, and the caller clears the cookies either way."""
        try:
            if access_token and await self._sign_out(access_token):
                return
            if refresh_token:
                # The access token is missing or expired, and Supabase needs a valid one to sign out.
                session = await self._supabase.refresh(refresh_token)
                await self._supabase.sign_out(session.access_token)
        except SupabaseError as err:
            logger.warning("sign-out failed", extra={"error": err.message})

    async def _sign_out(self, access_token: str) -> bool:
        try:
            await self._supabase.sign_out(access_token)
        except SupabaseRejectedError:
            return False
        return True

    def _verify_state(self, state_cookie: str | None) -> OAuthState:
        if not state_cookie:
            raise LoginError("invalid_state", "Missing OAuth state")
        try:
            return OAuthState.model_validate(self._state_signer.loads(state_cookie, max_age=OAUTH_STATE_TTL_SECONDS))
        except (InvalidTokenError, ValidationError) as err:
            raise LoginError("invalid_state", "Invalid or expired OAuth state") from err

    async def _upsert_user(self, session: SupabaseSession, github_user: GithubUser) -> UserRead:
        # The GitHub account identifies the person. Supabase's id for it changes if the Supabase user is
        # deleted and signs in again, so a miss falls back to the GitHub id and relinks that row.
        existing = await self._users.get_by_auth_user_id(session.user.id) or await self._users.get_by_github_id(
            github_user.id
        )
        if existing is None:
            user = await self._users.create(
                UserCreate(
                    auth_user_id=session.user.id,
                    github_id=github_user.id,
                    login=github_user.login,
                    name=github_user.name,
                    avatar_url=github_user.avatar_url,
                )
            )
        else:
            user = await self._users.update(
                existing,
                UserUpdate(
                    auth_user_id=session.user.id,
                    login=github_user.login,
                    name=github_user.name,
                    avatar_url=github_user.avatar_url,
                ),
            )
        return UserRead.model_validate(user)

    async def _save_credentials(self, user: UserRead, access_token: str) -> None:
        await self._credentials.upsert(
            GithubCredentialUpsert(user_id=user.id, access_token=self._cipher.encrypt(access_token))
        )
