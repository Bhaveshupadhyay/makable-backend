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
from app.schemas.github import (
    GithubBranchHead,
    GithubDirEntry,
    GithubFile,
    GithubRepo,
    GithubTreeChange,
    GithubUser,
)
from app.schemas.supabase import SupabaseSession, SupabaseUser
from app.services.workspace_service import git_blob_sha

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
    """An in-memory GitHub: users' repos with a branch, commits and files. Like GitHub, a file write must send the
    SHA of the version it replaces, and a commit must be based on the branch's current head."""

    def __init__(self) -> None:
        self.user = GithubUser(
            id=583231, login="octocat", name="The Octocat", avatar_url="https://github.com/octocat.png"
        )
        self.tokens_seen: list[str] = []
        self.repos: dict[str, GithubRepo] = {}
        # The files on each repo's branch, its head commit, and every tree by SHA.
        self.files: dict[str, dict[str, GithubFile]] = {}
        self.heads: dict[str, GithubBranchHead] = {}
        self.trees: dict[str, dict[str, GithubFile]] = {}
        # Writes to a new repo fail with 404 this many times, like a repo GitHub is still setting up.
        self.not_ready_writes = 0
        # Commits: (repo, message, changed paths).
        self.commits: list[tuple[str, str, list[str]]] = []
        # How often the workspace repo was looked up (the check a cache saves).
        self.repo_lookups = 0
        # Raised by the next write, e.g. a rate limit.
        self.fail_next_write: Exception | None = None
        # The branch moves this many times just before a commit lands (another save got in first).
        self.races = 0

    async def get_user(self, access_token: str) -> GithubUser:
        self.tokens_seen.append(access_token)
        return self.user

    async def get_repo(self, access_token: str, full_name: str) -> GithubRepo | None:
        self.tokens_seen.append(access_token)
        self.repo_lookups += 1
        return self.repos.get(full_name)

    async def create_private_repo(self, access_token: str, name: str, description: str) -> GithubRepo:
        if f"{self.user.login}/{name}" in self.repos:
            raise GithubRepoExistsError()
        return self.add_repo(name, private=True, files={"README.md": f"# {name}\n"})

    async def get_file(self, access_token: str, full_name: str, path: str) -> GithubFile | None:
        return self.files.get(full_name, {}).get(path)

    async def list_dir(self, access_token: str, full_name: str, path: str) -> list[GithubDirEntry]:
        prefix = f"{path}/"
        files = self.files.get(full_name, {})
        names = sorted({p[len(prefix) :].split("/")[0] for p in files if p.startswith(prefix)})
        return [GithubDirEntry(name=n, path=prefix + n, type="file" if prefix + n in files else "dir") for n in names]

    async def put_file(
        self, access_token: str, full_name: str, path: str, content: str, *, message: str, sha: str | None
    ) -> str:
        self._before_write(full_name)
        if self.not_ready_writes:
            self.not_ready_writes -= 1
            raise GithubNotFoundError()
        current = self.files[full_name].get(path)
        if (current.sha if current else None) != sha:
            raise GithubConflictError()
        self._commit(full_name, message, {**self.files[full_name], path: _file(content)}, [path])
        return self.files[full_name][path].sha

    async def get_branch_head(self, access_token: str, full_name: str, branch: str) -> GithubBranchHead:
        if full_name not in self.repos or branch != self.repos[full_name].default_branch:
            raise GithubNotFoundError()
        return self.heads[full_name]

    async def commit_changes(
        self,
        access_token: str,
        full_name: str,
        branch: str,
        head: GithubBranchHead,
        changes: list[GithubTreeChange],
        *,
        message: str,
    ) -> str:
        self._before_write(full_name)
        if self.races:
            self.races -= 1
            self._commit(full_name, "Another save", dict(self.files[full_name]), [])
        if head != self.heads[full_name]:
            raise GithubConflictError()
        files = dict(self.trees[head.tree_sha])
        for change in changes:
            if change.content is None:
                files.pop(change.path, None)
            else:
                files[change.path] = _file(change.content)
        self._commit(full_name, message, files, [c.path for c in changes])
        return self.heads[full_name].commit_sha

    def add_repo(self, name: str, *, private: bool, files: dict[str, str] | None = None) -> GithubRepo:
        """Sets up a repo directly, as if the user made it on GitHub."""
        full_name = f"{self.user.login}/{name}"
        repo = GithubRepo(
            id=len(self.repos) + 1,
            name=name,
            full_name=full_name,
            private=private,
            html_url=f"https://github.com/{full_name}",
            default_branch="main",
        )
        self.repos[full_name] = repo
        self._commit(full_name, "Initial commit", {p: _file(t) for p, t in (files or {}).items()}, [], record=False)
        return repo

    def _before_write(self, full_name: str) -> None:
        if self.fail_next_write is not None:
            error, self.fail_next_write = self.fail_next_write, None
            raise error
        if full_name not in self.repos:
            raise GithubNotFoundError()

    def _commit(
        self, full_name: str, message: str, files: dict[str, GithubFile], paths: list[str], *, record: bool = True
    ) -> None:
        tree = hashlib.sha1(repr(sorted((p, f.sha) for p, f in files.items())).encode(), usedforsecurity=False)
        parent = self.heads[full_name].commit_sha if full_name in self.heads else ""
        commit = hashlib.sha1(f"{parent}{tree.hexdigest()}{message}".encode(), usedforsecurity=False).hexdigest()
        self.trees[tree.hexdigest()] = files
        self.heads[full_name] = GithubBranchHead(commit_sha=commit, tree_sha=tree.hexdigest())
        self.files[full_name] = files
        if record:
            self.commits.append((full_name, message, paths))


def _file(content: str) -> GithubFile:
    """A file with the SHA git gives that content."""
    return GithubFile(content=content, sha=git_blob_sha(content))


class FakeModelClient:
    """Answers with scripted replies, in order, and records the messages it was sent."""

    name = "fake-model"

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.calls: list[list[ChatMessage]] = []

    async def complete(self, messages: list[ChatMessage]) -> str:
        self.calls.append(list(messages))
        return self.replies.pop(0)
