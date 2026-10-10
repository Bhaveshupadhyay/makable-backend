import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet

from app.constants.auth import Role
from app.constants.workspace import MARKER_PATH, session_path
from app.core.security import TokenCipher
from app.models.github_credential import GithubCredential
from app.schemas.github import GithubCredentialUpsert
from app.schemas.session import SessionSnapshot
from app.schemas.user import UserRead
from app.schemas.workspace import WorkspaceSessionWrite
from app.services.workspace_service import (
    GithubNotConnectedError,
    VerifiedRepos,
    WorkspaceConflictError,
    WorkspaceInvalidError,
    WorkspaceRepoPublicError,
    WorkspaceRepoTakenError,
    WorkspaceService,
    WorkspaceSessionNotFoundError,
    WorkspaceSessionTooLargeError,
)
from app.tests.fakes import FakeGithubClient
from app.tests.session_fixtures import PROJECT_ID, snapshot_body

CIPHER = TokenCipher(Fernet.generate_key().decode())
USER = UserRead(id=uuid4(), login="octocat", name=None, avatar_url="https://github.com/octocat.png", role=Role.USER)
REPO = "octocat/makable-workspace"


class FakeCredentials:
    def __init__(self, stored: str | None = "gho_token") -> None:
        self.token = stored

    async def get(self, user_id: UUID) -> GithubCredential | None:
        if self.token is None:
            return None
        return GithubCredential(user_id=user_id, access_token=CIPHER.encrypt(self.token))

    async def upsert(self, data: GithubCredentialUpsert) -> GithubCredential:
        raise NotImplementedError


async def no_sleep(_: float) -> None:
    return None


def service(
    github: FakeGithubClient, stored: str | None = "gho_token", verified: VerifiedRepos | None = None
) -> WorkspaceService:
    return WorkspaceService(
        github,
        FakeCredentials(stored),
        CIPHER,
        sleep=no_sleep,
        retry_delays=(0.1, 0.1),
        verified=verified or VerifiedRepos(),
    )


def write(base_sha: str | None = None, **over: Any) -> WorkspaceSessionWrite:
    return WorkspaceSessionWrite(snapshot=SessionSnapshot.model_validate(snapshot_body(**over)), base_sha=base_sha)


async def test_the_first_save_creates_the_private_repo_with_its_marker() -> None:
    github = FakeGithubClient()
    github.not_ready_writes = 2  # GitHub takes a moment before a new repo accepts writes.

    saved = await service(github).save_session(USER, PROJECT_ID, write())

    assert github.repos[REPO].private
    assert MARKER_PATH in github.files[REPO]
    stored = github.files[REPO][session_path(str(PROJECT_ID))]
    assert saved.sha == stored.sha
    assert saved.repo_url == "https://github.com/octocat/makable-workspace"
    assert github.tokens_seen[0] == "gho_token"
    # The file is what the SPA sent, so it imports back unchanged.
    assert json.loads(stored.content) == snapshot_body()


async def test_saves_need_the_sha_they_replace() -> None:
    github = FakeGithubClient()
    workspace = service(github)
    first = await workspace.save_session(USER, PROJECT_ID, write())

    later = write(base_sha=first.sha, exported_at="2026-10-10T13:00:00.000Z")
    second = await workspace.save_session(USER, PROJECT_ID, later)
    assert second.sha != first.sha

    # Another device saved in between: this one is based on an older version, so nothing is overwritten.
    with pytest.raises(WorkspaceConflictError) as conflict:
        await workspace.save_session(USER, PROJECT_ID, write(base_sha=first.sha))
    assert conflict.value.details == {"sha": second.sha, "exportedAt": "2026-10-10T13:00:00.000Z"}
    # A first save from a new device when one already exists is a conflict too.
    with pytest.raises(WorkspaceConflictError):
        await workspace.save_session(USER, PROJECT_ID, write(base_sha=None))


async def test_reads_a_session_back_and_finds_the_newest() -> None:
    github = FakeGithubClient()
    workspace = service(github)
    other = UUID("11111111-2222-4333-8444-555555555555")
    with pytest.raises(WorkspaceSessionNotFoundError):
        await workspace.get_latest_session(USER)

    await workspace.save_session(USER, PROJECT_ID, write(exported_at="2026-10-10T12:00:00.000Z"))
    newer = await workspace.save_session(USER, other, write(project_id=other, exported_at="2026-10-11T09:00:00.000Z"))

    latest = await workspace.get_latest_session(USER)
    assert (latest.snapshot.project_id, latest.sha) == (other, newer.sha)
    assert (await workspace.get_session(USER, PROJECT_ID)).snapshot.project_id == PROJECT_ID
    with pytest.raises(WorkspaceSessionNotFoundError):
        await workspace.get_session(USER, uuid4())


async def test_never_writes_to_a_repo_makable_did_not_create_or_that_is_public() -> None:
    taken = FakeGithubClient()
    taken.add_repo("makable-workspace", private=True, files={"README.md": "mine"})
    with pytest.raises(WorkspaceRepoTakenError):
        await service(taken).save_session(USER, PROJECT_ID, write())
    assert taken.writes == []

    public = FakeGithubClient()
    public.add_repo("makable-workspace", private=False, files={MARKER_PATH: "{}"})
    with pytest.raises(WorkspaceRepoPublicError):
        await service(public).get_latest_session(USER)
    assert public.writes == []


async def test_rejects_sessions_for_another_site_too_large_or_without_a_token() -> None:
    github = FakeGithubClient()
    with pytest.raises(WorkspaceInvalidError):
        await service(github).save_session(USER, uuid4(), write())
    big = {"minimal": {f"src/f{i}.css": "x" * 60_000 for i in range(90)}}
    with pytest.raises(WorkspaceSessionTooLargeError):
        await service(github).save_session(USER, PROJECT_ID, write(fileEdits=big))
    with pytest.raises(GithubNotConnectedError):
        await service(github, stored=None).save_session(USER, PROJECT_ID, write())
    assert github.writes == []


async def test_a_session_file_edited_into_garbage_counts_as_missing() -> None:
    github = FakeGithubClient()
    github.add_repo(
        "makable-workspace", private=True, files={MARKER_PATH: "{}", session_path(str(PROJECT_ID)): "{not json"}
    )
    with pytest.raises(WorkspaceSessionNotFoundError):
        await service(github).get_latest_session(USER)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_a_checked_repo_is_trusted_until_it_expires() -> None:
    github = FakeGithubClient()
    clock = FakeClock()
    workspace = service(github, verified=VerifiedRepos(ttl=600, clock=clock))
    first = await workspace.save_session(USER, PROJECT_ID, write())
    lookups = github.repo_lookups

    # Within the expiry, saves go straight to the repo: no lookup, no marker read.
    later = write(base_sha=first.sha, exported_at="2026-10-10T12:01:00Z")
    second = await workspace.save_session(USER, PROJECT_ID, later)
    assert github.repo_lookups == lookups

    # After it, the repo is checked again, so a repo made public meanwhile is caught.
    github.repos[REPO] = github.repos[REPO].model_copy(update={"private": False})
    clock.now += 601
    with pytest.raises(WorkspaceRepoPublicError):
        await workspace.save_session(USER, PROJECT_ID, write(base_sha=second.sha, exported_at="2026-10-10T12:20:00Z"))


async def test_a_cached_repo_deleted_on_github_is_created_again() -> None:
    github = FakeGithubClient()
    workspace = service(github)
    await workspace.save_session(USER, PROJECT_ID, write())
    del github.repos[REPO], github.files[REPO]

    saved = await workspace.save_session(USER, PROJECT_ID, write(exported_at="2026-10-10T13:00:00Z"))

    assert github.repos[REPO].private
    assert MARKER_PATH in github.files[REPO]
    assert github.files[REPO][session_path(str(PROJECT_ID))].sha == saved.sha
