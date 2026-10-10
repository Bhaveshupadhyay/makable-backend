import json
from typing import Any
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet

from app.constants.auth import Role
from app.constants.workspace import MARKER_PATH
from app.core.security import TokenCipher
from app.models.github_credential import GithubCredential
from app.schemas.github import GithubCredentialUpsert
from app.schemas.user import UserRead
from app.schemas.workspace import WorkspaceSave
from app.services.workspace_service import (
    GithubNotConnectedError,
    VerifiedRepos,
    WorkspaceConflictError,
    WorkspaceInvalidError,
    WorkspacePartNotFoundError,
    WorkspaceRepoPublicError,
    WorkspaceRepoTakenError,
    WorkspaceService,
    WorkspaceSessionNotFoundError,
    git_blob_sha,
)
from app.tests.fakes import FakeGithubClient
from app.tests.session_fixtures import AI_HISTORY, PROJECT_ID, chunk, save_body

CIPHER = TokenCipher(Fernet.generate_key().decode())
USER = UserRead(id=uuid4(), login="octocat", name=None, avatar_url="https://github.com/octocat.png", role=Role.USER)
REPO = "octocat/makable-workspace"
DIR = f"projects/{PROJECT_ID}"


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


def save(**over: Any) -> WorkspaceSave:
    return WorkspaceSave.model_validate(save_body(**over))


async def test_the_first_save_creates_the_private_repo_and_writes_the_session_as_one_commit() -> None:
    github = FakeGithubClient()
    github.not_ready_writes = 2  # GitHub takes a moment before a new repo accepts writes.

    saved = await service(github).save(USER, PROJECT_ID, save())

    assert github.repos[REPO].private
    assert MARKER_PATH in github.files[REPO]
    files = github.files[REPO]
    # The parts are written exactly as sent, next to the state the server wrote.
    assert files[f"{DIR}/messages/0001.json"].content == chunk("I want a portfolio", "Use Minimal")
    assert files[f"{DIR}/ai-history/minimal.json"].content == AI_HISTORY
    assert files[f"{DIR}/files/minimal/src/index.css"].content == "a { color: red }\n"
    assert json.loads(files[f"{DIR}/state.json"].content)["messageChunks"] == 1
    # The returned SHA is the one GitHub gives the state file: the next save sends it back.
    assert saved.sha == files[f"{DIR}/state.json"].sha
    assert saved.repo_url == "https://github.com/octocat/makable-workspace"
    # Marker, then one commit with every file of the session.
    assert [c[2] for c in github.commits][-1] == [
        f"{DIR}/messages/0001.json",
        f"{DIR}/ai-history/minimal.json",
        f"{DIR}/files/minimal/src/index.css",
        f"{DIR}/state.json",
    ]


async def test_later_saves_write_only_what_changed_and_remove_what_went_away() -> None:
    github = FakeGithubClient()
    workspace = service(github)
    first = await workspace.save(USER, PROJECT_ID, save())

    more = [{"path": "messages/0002.json", "content": chunk("Make it bigger")}]
    second = await workspace.save(
        USER,
        PROJECT_ID,
        save(
            base_sha=first.sha,
            parts=more,
            deletes=["files/minimal/src/index.css"],
            saved_at="2026-10-10T12:05:00.000Z",
            messageChunks=2,
            files={},
        ),
    )

    assert github.commits[-1][2] == [
        f"{DIR}/messages/0002.json",
        f"{DIR}/files/minimal/src/index.css",
        f"{DIR}/state.json",
    ]
    files = github.files[REPO]
    assert f"{DIR}/messages/0001.json" in files  # untouched, still there
    assert f"{DIR}/files/minimal/src/index.css" not in files
    assert second.sha == files[f"{DIR}/state.json"].sha != first.sha


async def test_a_big_save_goes_up_in_batches_with_the_state_last() -> None:
    github = FakeGithubClient()
    workspace = service(github)
    body = save_body()

    batch = await workspace.save(USER, PROJECT_ID, WorkspaceSave.model_validate({**body, "state": None}))
    # The files are there, but no state yet: a restore wouldn't find this session half-saved.
    assert batch.sha is None
    assert f"{DIR}/messages/0001.json" in github.files[REPO]
    assert f"{DIR}/state.json" not in github.files[REPO]

    last = await workspace.save(USER, PROJECT_ID, WorkspaceSave.model_validate({**body, "parts": []}))
    assert last.sha == github.files[REPO][f"{DIR}/state.json"].sha
    with pytest.raises(WorkspaceInvalidError):
        await workspace.save(USER, PROJECT_ID, WorkspaceSave.model_validate({"baseSha": last.sha}))


async def test_a_save_based_on_an_older_state_is_a_conflict_and_writes_nothing() -> None:
    github = FakeGithubClient()
    workspace = service(github)
    first = await workspace.save(USER, PROJECT_ID, save())
    second = await workspace.save(USER, PROJECT_ID, save(base_sha=first.sha, saved_at="2026-10-10T13:00:00.000Z"))
    commits = len(github.commits)

    with pytest.raises(WorkspaceConflictError) as conflict:
        await workspace.save(USER, PROJECT_ID, save(base_sha=first.sha))
    assert conflict.value.details == {"sha": second.sha, "savedAt": "2026-10-10T13:00:00.000Z"}
    # A first save from a new device, when one already exists, is a conflict too.
    with pytest.raises(WorkspaceConflictError):
        await workspace.save(USER, PROJECT_ID, save(base_sha=None))
    assert len(github.commits) == commits


async def test_a_branch_that_moves_mid_save_is_retried_once() -> None:
    github = FakeGithubClient()
    workspace = service(github)
    first = await workspace.save(USER, PROJECT_ID, save())

    github.races = 1  # another site's save lands just before this commit
    await workspace.save(USER, PROJECT_ID, save(base_sha=first.sha, saved_at="2026-10-10T12:01:00.000Z"))
    assert json.loads(github.files[REPO][f"{DIR}/state.json"].content)["savedAt"] == "2026-10-10T12:01:00.000Z"

    github.races = 2
    current = github.files[REPO][f"{DIR}/state.json"].sha
    with pytest.raises(WorkspaceConflictError):
        await workspace.save(USER, PROJECT_ID, save(base_sha=current, saved_at="2026-10-10T12:02:00.000Z"))


async def test_reads_the_newest_state_and_each_part() -> None:
    github = FakeGithubClient()
    workspace = service(github)
    other = UUID("11111111-2222-4333-8444-555555555555")
    with pytest.raises(WorkspaceSessionNotFoundError):
        await workspace.get_latest_state(USER)

    await workspace.save(USER, PROJECT_ID, save(saved_at="2026-10-10T12:00:00.000Z"))
    newer = await workspace.save(USER, other, save(project_id=other, saved_at="2026-10-11T09:00:00.000Z"))

    latest = await workspace.get_latest_state(USER)
    assert (latest.state.project_id, latest.sha) == (other, newer.sha)
    assert (await workspace.get_state(USER, PROJECT_ID)).state.project_id == PROJECT_ID
    part = await workspace.get_part(USER, PROJECT_ID, "messages/0001.json")
    assert part.content == chunk("I want a portfolio", "Use Minimal")
    with pytest.raises(WorkspacePartNotFoundError):
        await workspace.get_part(USER, PROJECT_ID, "messages/0002.json")
    with pytest.raises(WorkspaceInvalidError):
        await workspace.get_part(USER, PROJECT_ID, "../../.makable-workspace.json")
    with pytest.raises(WorkspaceSessionNotFoundError):
        await workspace.get_state(USER, uuid4())


async def test_never_writes_to_a_repo_makable_did_not_create_or_that_is_public() -> None:
    taken = FakeGithubClient()
    taken.add_repo("makable-workspace", private=True, files={"README.md": "mine"})
    with pytest.raises(WorkspaceRepoTakenError):
        await service(taken).save(USER, PROJECT_ID, save())
    assert taken.commits == []

    public = FakeGithubClient()
    public.add_repo("makable-workspace", private=False, files={MARKER_PATH: "{}"})
    with pytest.raises(WorkspaceRepoPublicError):
        await service(public).get_latest_state(USER)
    assert public.commits == []


async def test_rejects_saves_for_another_site_or_without_a_token() -> None:
    github = FakeGithubClient()
    with pytest.raises(WorkspaceInvalidError):
        await service(github).save(USER, uuid4(), save())
    with pytest.raises(GithubNotConnectedError):
        await service(github, stored=None).save(USER, PROJECT_ID, save())
    assert github.commits == []


async def test_a_state_edited_into_garbage_counts_as_missing() -> None:
    github = FakeGithubClient()
    github.add_repo("makable-workspace", private=True, files={MARKER_PATH: "{}", f"{DIR}/state.json": "{not json"})
    with pytest.raises(WorkspaceSessionNotFoundError):
        await service(github).get_latest_state(USER)


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_a_checked_repo_is_trusted_until_it_expires() -> None:
    github = FakeGithubClient()
    clock = FakeClock()
    workspace = service(github, verified=VerifiedRepos(ttl=600, clock=clock))
    first = await workspace.save(USER, PROJECT_ID, save())
    lookups = github.repo_lookups

    # Within the expiry, saves skip the repo and marker checks.
    later = save(base_sha=first.sha, saved_at="2026-10-10T12:01:00.000Z")
    second = await workspace.save(USER, PROJECT_ID, later)
    assert github.repo_lookups == lookups

    # After it, the repo is checked again, so a repo made public meanwhile is caught.
    github.repos[REPO] = github.repos[REPO].model_copy(update={"private": False})
    clock.now += 601
    with pytest.raises(WorkspaceRepoPublicError):
        await workspace.save(USER, PROJECT_ID, save(base_sha=second.sha, saved_at="2026-10-10T12:20:00.000Z"))


async def test_a_cached_repo_deleted_on_github_is_created_again() -> None:
    github = FakeGithubClient()
    workspace = service(github)
    await workspace.save(USER, PROJECT_ID, save())
    del github.repos[REPO], github.files[REPO], github.heads[REPO]

    saved = await workspace.save(USER, PROJECT_ID, save(saved_at="2026-10-10T13:00:00.000Z"))

    assert github.repos[REPO].private
    assert MARKER_PATH in github.files[REPO]
    assert github.files[REPO][f"{DIR}/state.json"].sha == saved.sha


def test_git_blob_sha_matches_git() -> None:
    # `printf 'hello\n' | git hash-object --stdin`
    assert git_blob_sha("hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"
