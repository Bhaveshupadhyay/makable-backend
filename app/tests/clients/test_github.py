"""The real GitHub client against a mocked transport: request shapes and status mapping."""

import base64
import json
import time
from collections.abc import Callable

import httpx
import pytest

from app.clients.github import (
    GithubConflictError,
    GithubError,
    GithubNotFoundError,
    GithubPagesUnavailableError,
    GithubRateLimitedError,
    GithubRepoExistsError,
    GithubUnauthorizedError,
    HttpGithubClient,
)
from app.core.limits import ServiceBusyError
from app.schemas.github import GithubBranchHead, GithubTreeChange

REPO = "octocat/makable-workspace"


def client(handler: Callable[[httpx.Request], httpx.Response]) -> HttpGithubClient:
    return HttpGithubClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


async def test_put_file_sends_base64_and_the_sha_and_returns_the_new_sha() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"content": {"sha": "b" * 40}})

    sha = await client(handler).put_file("tok", REPO, "projects/a b/session.json", "{}", message="Save", sha="a" * 40)

    assert sha == "b" * 40
    [request] = seen
    assert request.method == "PUT"
    assert request.url.raw_path == b"/repos/octocat/makable-workspace/contents/projects/a%20b/session.json"
    assert request.headers["authorization"] == "Bearer tok"
    assert json.loads(request.content) == {"message": "Save", "content": b64("{}"), "sha": "a" * 40}


@pytest.mark.parametrize(
    ("response", "sha", "error"),
    [
        (httpx.Response(409), "a" * 40, GithubConflictError),
        (httpx.Response(422), None, GithubConflictError),
        (httpx.Response(404), None, GithubNotFoundError),
        (httpx.Response(401), None, GithubUnauthorizedError),
        (httpx.Response(403, headers={"x-ratelimit-remaining": "0"}), None, GithubRateLimitedError),
    ],
)
async def test_put_file_maps_errors(response: httpx.Response, sha: str | None, error: type[Exception]) -> None:
    with pytest.raises(error):
        await client(lambda _: response).put_file("tok", REPO, "a.json", "{}", message="m", sha=sha)


async def test_get_file_decodes_content_and_reads_large_files_as_blobs() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/small.json"):
            return httpx.Response(200, json={"type": "file", "sha": "s1", "encoding": "base64", "content": b64("hé\n")})
        if request.url.path.endswith("/big.json"):
            return httpx.Response(200, json={"type": "file", "sha": "s2", "encoding": "none", "content": ""})
        if request.url.path.endswith("/git/blobs/s2"):
            return httpx.Response(200, json={"content": b64("big"), "encoding": "base64"})
        return httpx.Response(404)

    github = client(handler)
    small = await github.get_file("tok", REPO, "small.json")
    big = await github.get_file("tok", REPO, "big.json")

    assert small is not None and (small.content, small.sha) == ("hé\n", "s1")
    assert big is not None and (big.content, big.sha) == ("big", "s2")
    assert await github.get_file("tok", REPO, "missing.json") is None


async def test_create_repo_and_an_existing_name() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) > 1:
            return httpx.Response(422)
        repo = {
            "id": 1,
            "name": "makable-workspace",
            "full_name": REPO,
            "private": True,
            "html_url": "https://x",
            "default_branch": "main",
        }
        return httpx.Response(201, json=repo)

    github = client(handler)
    repo = await github.create_repo("tok", "makable-workspace", "desc", private=True)
    assert repo.private
    assert bodies[0]["private"] is True and bodies[0]["auto_init"] is True
    with pytest.raises(GithubRepoExistsError):
        await github.create_repo("tok", "makable-workspace", "desc", private=True)


@pytest.mark.parametrize(
    ("headers", "reset_in", "expected"),
    [
        ({"retry-after": "120"}, None, 120),
        ({"x-ratelimit-remaining": "0"}, 300, 300),
        ({"x-ratelimit-remaining": "0"}, None, 60),
    ],
)
async def test_rate_limits_carry_githubs_wait(headers: dict[str, str], reset_in: int | None, expected: int) -> None:
    if reset_in is not None:
        headers = {**headers, "x-ratelimit-reset": str(int(time.time()) + reset_in)}
    github = client(lambda _: httpx.Response(403, headers=headers))
    with pytest.raises(GithubRateLimitedError) as limited:
        await github.put_file("tok", REPO, "a.json", "{}", message="m", sha=None)
    assert abs(limited.value.retry_after - expected) <= 2
    assert limited.value.details == {"retryAfter": limited.value.retry_after}


async def test_commit_changes_writes_one_tree_and_commit_then_moves_the_branch() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/git/trees"):
            return httpx.Response(201, json={"sha": "t2"})
        if request.url.path.endswith("/git/commits"):
            return httpx.Response(201, json={"sha": "c2"})
        return httpx.Response(200, json={"object": {"sha": "c2"}})

    head = GithubBranchHead(commit_sha="c1", tree_sha="t1")
    changes = [GithubTreeChange(path="a.json", content="{}"), GithubTreeChange(path="old.css", content=None)]
    sha = await client(handler).commit_changes("tok", REPO, "main", head, changes, message="Save")

    assert sha == "c2"
    tree, commit, ref = (json.loads(r.content) for r in seen)
    assert tree == {
        "base_tree": "t1",
        "tree": [
            {"path": "a.json", "mode": "100644", "type": "blob", "content": "{}"},
            {"path": "old.css", "mode": "100644", "type": "blob", "sha": None},
        ],
    }
    assert commit == {"message": "Save", "tree": "t2", "parents": ["c1"]}
    assert seen[2].method == "PATCH" and seen[2].url.path.endswith("/git/refs/heads/main")
    assert ref == {"sha": "c2", "force": False}


async def test_a_branch_that_moved_is_a_conflict_and_the_head_is_read_in_one_request() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/branches/main"):
            return httpx.Response(200, json={"commit": {"sha": "c1", "commit": {"tree": {"sha": "t1"}}}})
        if request.method == "PATCH":
            return httpx.Response(422, json={"message": "Update is not a fast forward"})
        return httpx.Response(201, json={"sha": "x"})

    github = client(handler)
    head = await github.get_branch_head("tok", REPO, "main")
    assert head == GithubBranchHead(commit_sha="c1", tree_sha="t1")
    with pytest.raises(GithubConflictError):
        await github.commit_changes("tok", REPO, "main", head, [], message="m")


async def test_no_free_connection_is_a_quick_busy_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.PoolTimeout("no free connection")

    with pytest.raises(ServiceBusyError):
        await client(handler).get_repo("tok", REPO)


async def test_get_scopes_reads_the_oauth_header() -> None:
    github = client(lambda _: httpx.Response(200, json={}, headers={"x-oauth-scopes": "repo, workflow"}))
    assert await github.get_scopes("tok") == {"repo", "workflow"}
    assert await client(lambda _: httpx.Response(200, json={})).get_scopes("tok") is None


async def test_commit_changes_uploads_binary_files_as_blobs() -> None:
    bodies: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        bodies[request.url.path] = json.loads(request.content)
        return httpx.Response(201, json={"sha": "c" * 40, "ref": "refs/heads/main"})

    head = GithubBranchHead(commit_sha="a" * 40, tree_sha="b" * 40)
    changes = [
        GithubTreeChange(path="a.png", content=b"\x89PNG"),
        GithubTreeChange(path="index.html", content="<main></main>"),
    ]
    await client(handler).commit_changes("tok", REPO, "main", head, changes, message="Publish")

    assert bodies[f"/repos/{REPO}/git/blobs"] == {
        "content": base64.b64encode(b"\x89PNG").decode(),
        "encoding": "base64",
    }
    assert bodies[f"/repos/{REPO}/git/trees"] == {
        "base_tree": "b" * 40,
        "tree": [
            {"path": "a.png", "mode": "100644", "type": "blob", "sha": "c" * 40},
            {"path": "index.html", "mode": "100644", "type": "blob", "content": "<main></main>"},
        ],
    }


async def test_get_tree_lists_files_and_refuses_a_truncated_tree() -> None:
    tree = [{"path": "src", "type": "tree", "sha": "1"}, {"path": "src/a.ts", "type": "blob", "sha": "2"}]
    github = client(lambda _: httpx.Response(200, json={"tree": tree, "truncated": False}))
    assert await github.get_tree("tok", REPO, "t") == {"src/a.ts": "2"}
    with pytest.raises(GithubError):
        await client(lambda _: httpx.Response(200, json={"tree": [], "truncated": True})).get_tree("tok", REPO, "t")


async def test_actions_secrets() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/public-key"):
            return httpx.Response(200, json={"key_id": "k1", "key": "S0VZ"})
        if request.method == "PUT":
            return httpx.Response(201)
        return httpx.Response(404 if request.url.path.endswith("/MISSING") else 200, json={"name": "X"})

    github = client(handler)
    key = await github.get_actions_key("tok", REPO)
    await github.put_actions_secret("tok", REPO, "VERCEL_TOKEN", "c2VhbGVk", key.key_id)

    assert json.loads(seen[1].content) == {"encrypted_value": "c2VhbGVk", "key_id": "k1"}
    assert seen[1].url.path == f"/repos/{REPO}/actions/secrets/VERCEL_TOKEN"
    assert await github.has_actions_secret("tok", REPO, "VERCEL_TOKEN")
    assert not await github.has_actions_secret("tok", REPO, "MISSING")


async def test_enable_pages_switches_an_existing_site_to_workflow_builds() -> None:
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        assert json.loads(request.content) == {"build_type": "workflow"}
        return httpx.Response(409 if request.method == "POST" else 204)

    await client(handler).enable_pages_workflow("tok", REPO)
    assert methods == ["POST", "PUT"]
    with pytest.raises(GithubPagesUnavailableError):
        await client(lambda _: httpx.Response(422)).enable_pages_workflow("tok", REPO)


async def test_workflow_dispatch_and_runs() -> None:
    run = {
        "status": "completed",
        "conclusion": "success",
        "head_sha": "a" * 40,
        "html_url": "https://github.com/x/actions/runs/1",
        "created_at": "2026-10-11T10:00:00Z",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            assert json.loads(request.content) == {"ref": "main"}
            return httpx.Response(204)
        assert request.url.params["branch"] == "main"
        return httpx.Response(200, json={"workflow_runs": [run]})

    github = client(handler)
    await github.dispatch_workflow("tok", REPO, "makable-deploy.yml", "main")
    latest = await github.latest_workflow_run("tok", REPO, "makable-deploy.yml", "main")
    assert latest is not None and latest.conclusion == "success"
    with pytest.raises(GithubNotFoundError):
        await client(lambda _: httpx.Response(404)).dispatch_workflow("tok", REPO, "x.yml", "main")
    assert (
        await client(lambda _: httpx.Response(200, json={"workflow_runs": []})).latest_workflow_run(
            "tok", REPO, "x.yml", "main"
        )
        is None
    )


async def test_get_blob_reads_text_by_sha() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/{REPO}/git/blobs/{'a' * 40}"
        return httpx.Response(200, json={"content": b64("body {}\n"), "encoding": "base64"})

    assert await client(handler).get_blob("tok", REPO, "a" * 40) == "body {}\n"
    with pytest.raises(GithubNotFoundError):
        await client(lambda _: httpx.Response(404)).get_blob("tok", REPO, "a" * 40)


async def test_runs_since_a_time_and_a_workflow_github_hasnt_registered() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"workflow_runs": []})

    await client(handler).latest_workflow_run(
        "tok", REPO, "makable-deploy.yml", "main", created_since="2026-10-11T13:45:07Z"
    )
    assert seen[0].url.params["created"] == ">=2026-10-11T13:45:07Z"
    # "Workflow does not have 'workflow_dispatch' trigger": GitHub still has the old file. Retried by the caller.
    with pytest.raises(GithubNotFoundError):
        await client(lambda _: httpx.Response(422)).dispatch_workflow("tok", REPO, "x.yml", "main")
