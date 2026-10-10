import hashlib
from datetime import timedelta
from urllib.parse import urlencode
from uuid import UUID, uuid4

from app.clients.github import GithubConflictError, GithubNotFoundError, GithubRepoExistsError
from app.clients.supabase import SupabaseRejectedError
from app.core.database import utc_now
from app.core.security import InvalidTokenError, generate_token, pkce_challenge
from app.schemas.auth import AccessTokenClaims
from app.schemas.chat import ChatMessage
from app.schemas.github import GithubDirEntry, GithubFile, GithubRepo, GithubUser
from app.schemas.supabase import SupabaseSession, SupabaseUser

VALID_CODE = "good-code"
GITHUB_TOKEN = "gho_access"


class FakeSupabaseClient:
    """Stands in for Supabase Auth. Like Supabase, it checks PKCE (the verifier sent with the code must hash
    to the challenge sent with the authorize URL), rotates refresh tokens, and on sign-out revokes the
    session's refresh tokens but not the access tokens already issued."""

    def __init__(self) -> None:
        self.auth_user_id = uuid4()
        self.provider_token: str | None = GITHUB_TOKEN
        self.signed_out: list[UUID] = []
        self._challenge: str | None = None
        # token -> session id
        self._access: dict[str, UUID] = {}
        self._refresh: dict[str, UUID] = {}

    def authorize_url(self, *, provider: str, scopes: str, redirect_to: str, code_challenge: str) -> str:
        self._challenge = code_challenge
        query = urlencode({"provider": provider, "scopes": scopes, "redirect_to": redirect_to})
        return f"https://project.supabase.co/auth/v1/authorize?{query}"

    async def exchange_code(self, *, auth_code: str, code_verifier: str) -> SupabaseSession:
        if auth_code != VALID_CODE or pkce_challenge(code_verifier) != self._challenge:
            raise SupabaseRejectedError("bad_code_verifier")
        return self._session(uuid4(), provider_token=self.provider_token)

    async def refresh(self, refresh_token: str) -> SupabaseSession:
        session_id = self._refresh.pop(refresh_token, None)
        if session_id is None:
            raise SupabaseRejectedError("refresh_token_not_found")
        return self._session(session_id)

    async def sign_out(self, access_token: str) -> None:
        session_id = self._access.get(access_token)
        if session_id is None:
            raise SupabaseRejectedError("bad_jwt")
        self.signed_out.append(session_id)
        self._refresh = {token: sid for token, sid in self._refresh.items() if sid != session_id}

    async def verify(self, access_token: str) -> AccessTokenClaims:
        if access_token not in self._access:
            raise InvalidTokenError("unknown token")
        return AccessTokenClaims(sub=self.auth_user_id, exp=utc_now() + timedelta(hours=1))

    def expire_access_tokens(self) -> None:
        self._access.clear()

    def _session(self, session_id: UUID, *, provider_token: str | None = None) -> SupabaseSession:
        access_token, refresh_token = generate_token(), generate_token()
        self._access[access_token] = session_id
        self._refresh[refresh_token] = session_id
        return SupabaseSession(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=3600,
            user=SupabaseUser(id=self.auth_user_id),
            provider_token=provider_token,
        )


class FakeGithubClient:
    """An in-memory GitHub: users' repos with files and blob SHAs. Like GitHub, a file write must send the SHA
    of the version it replaces, or it's refused as a conflict."""

    def __init__(self) -> None:
        self.user = GithubUser(
            id=583231, login="octocat", name="The Octocat", avatar_url="https://github.com/octocat.png"
        )
        self.tokens_seen: list[str] = []
        self.repos: dict[str, GithubRepo] = {}
        self.files: dict[str, dict[str, GithubFile]] = {}
        # Writes to a repo created this many writes ago fail with 404, like a repo GitHub is still setting up.
        self.not_ready_writes = 0
        self.writes: list[tuple[str, str, str]] = []
        # How often the workspace repo was looked up (the check a cache saves).
        self.repo_lookups = 0
        # Raised by the next file write, e.g. a rate limit.
        self.fail_next_write: Exception | None = None

    async def get_user(self, access_token: str) -> GithubUser:
        self.tokens_seen.append(access_token)
        return self.user

    async def get_repo(self, access_token: str, full_name: str) -> GithubRepo | None:
        self.tokens_seen.append(access_token)
        self.repo_lookups += 1
        return self.repos.get(full_name)

    async def create_private_repo(self, access_token: str, name: str, description: str) -> GithubRepo:
        full_name = f"{self.user.login}/{name}"
        if full_name in self.repos:
            raise GithubRepoExistsError()
        return self.add_repo(name, private=True)

    async def get_file(self, access_token: str, full_name: str, path: str) -> GithubFile | None:
        return self.files.get(full_name, {}).get(path)

    async def list_dir(self, access_token: str, full_name: str, path: str) -> list[GithubDirEntry]:
        prefix = f"{path}/"
        names = sorted({p[len(prefix) :].split("/")[0] for p in self.files.get(full_name, {}) if p.startswith(prefix)})
        return [
            GithubDirEntry(
                name=n,
                path=prefix + n,
                type="file" if f"{prefix}{n}" in self.files[full_name] else "dir",
            )
            for n in names
        ]

    async def put_file(
        self, access_token: str, full_name: str, path: str, content: str, *, message: str, sha: str | None
    ) -> str:
        if self.fail_next_write is not None:
            error, self.fail_next_write = self.fail_next_write, None
            raise error
        if full_name not in self.repos:
            raise GithubNotFoundError()
        if self.not_ready_writes:
            self.not_ready_writes -= 1
            raise GithubNotFoundError()
        current = self.files[full_name].get(path)
        if (current.sha if current else None) != sha:
            raise GithubConflictError()
        new_sha = hashlib.sha1(f"blob {content}".encode(), usedforsecurity=False).hexdigest()
        self.files[full_name][path] = GithubFile(content=content, sha=new_sha)
        self.writes.append((full_name, path, message))
        return new_sha

    def add_repo(self, name: str, *, private: bool, files: dict[str, str] | None = None) -> GithubRepo:
        """Sets up a repo directly, as if the user made it on GitHub."""
        full_name = f"{self.user.login}/{name}"
        repo = GithubRepo(
            id=len(self.repos) + 1,
            name=name,
            full_name=full_name,
            private=private,
            html_url=f"https://github.com/{full_name}",
        )
        self.repos[full_name] = repo
        self.files[full_name] = {
            path: GithubFile(content=text, sha=hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest())
            for path, text in (files or {}).items()
        }
        return repo


class FakeModelClient:
    """Answers with scripted replies, in order, and records the messages it was sent."""

    name = "fake-model"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[list[ChatMessage]] = []

    async def complete(self, messages: list[ChatMessage]) -> str:
        self.calls.append(list(messages))
        return self.replies.pop(0)
