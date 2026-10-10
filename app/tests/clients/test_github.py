"""The real GitHub client against a mocked transport: request shapes and status mapping."""

import base64
import json
import time
from collections.abc import Callable

import httpx
import pytest

from app.clients.github import (
    GithubConflictError,
    GithubNotFoundError,
    GithubRateLimitedError,
    GithubRepoExistsError,
    GithubUnauthorizedError,
    HttpGithubClient,
)

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


async def test_create_private_repo_and_an_existing_name() -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) > 1:
            return httpx.Response(422)
        repo = {"id": 1, "name": "makable-workspace", "full_name": REPO, "private": True, "html_url": "https://x"}
        return httpx.Response(201, json=repo)

    github = client(handler)
    repo = await github.create_private_repo("tok", "makable-workspace", "desc")
    assert repo.private
    assert bodies[0]["private"] is True and bodies[0]["auto_init"] is True
    with pytest.raises(GithubRepoExistsError):
        await github.create_private_repo("tok", "makable-workspace", "desc")


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
