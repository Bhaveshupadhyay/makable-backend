"""The hosting platforms' client against a mocked transport: request shapes and error mapping."""

import json
from collections.abc import Callable

import httpx
import pytest

from app.clients.deploy_platforms import (
    HttpDeployPlatformClient,
    PlatformAccountRequiredError,
    PlatformError,
    PlatformProjectTakenError,
    PlatformTokenInvalidError,
)
from app.constants.publish import PublishTarget

ACCOUNT = "a" * 32
Routes = dict[tuple[str, str], httpx.Response]


def client(routes: Routes, seen: list[httpx.Request] | None = None) -> HttpDeployPlatformClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        assert request.headers["authorization"] == "Bearer tok"
        return routes.get((request.method, str(request.url)), httpx.Response(404))

    return HttpDeployPlatformClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def prepare(c: HttpDeployPlatformClient, target: PublishTarget, account_id: str | None = None):  # type: ignore[no-untyped-def]
    return await c.prepare(target, "tok", project_name="octocat-portfolio", branch="main", account_id=account_id)


async def test_vercel_creates_a_missing_project() -> None:
    seen: list[httpx.Request] = []
    routes: Routes = {
        ("GET", "https://api.vercel.com/v2/user"): httpx.Response(200, json={"user": {}}),
        ("POST", "https://api.vercel.com/v11/projects"): httpx.Response(200, json={"id": "prj_1"}),
    }

    project = await prepare(client(routes, seen), PublishTarget.VERCEL)

    assert project.site_url == "https://octocat-portfolio.vercel.app"
    assert project.secrets == {"VERCEL_TOKEN": "tok"}
    assert json.loads(seen[-1].content) == {"name": "octocat-portfolio", "framework": None}


async def test_vercel_uses_an_existing_project_and_refuses_a_bad_token() -> None:
    routes: Routes = {
        ("GET", "https://api.vercel.com/v2/user"): httpx.Response(200, json={}),
        ("GET", "https://api.vercel.com/v9/projects/octocat-portfolio"): httpx.Response(200, json={"id": "prj_1"}),
    }
    seen: list[httpx.Request] = []
    await prepare(client(routes, seen), PublishTarget.VERCEL)
    assert [r.method for r in seen] == ["GET", "GET"]

    with pytest.raises(PlatformTokenInvalidError):
        await prepare(client({("GET", "https://api.vercel.com/v2/user"): httpx.Response(403)}), PublishTarget.VERCEL)


async def test_cloudflare_picks_the_only_account_and_creates_the_project() -> None:
    projects = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/pages/projects"
    seen: list[httpx.Request] = []
    routes: Routes = {
        ("GET", "https://api.cloudflare.com/client/v4/accounts"): httpx.Response(
            200, json={"result": [{"id": ACCOUNT, "name": "Octo"}]}
        ),
        ("POST", projects): httpx.Response(200, json={"result": {"subdomain": "octocat-portfolio-x1.pages.dev"}}),
    }

    project = await prepare(client(routes, seen), PublishTarget.CLOUDFLARE_PAGES)

    assert project.site_url == "https://octocat-portfolio-x1.pages.dev"
    assert project.secrets == {"CLOUDFLARE_API_TOKEN": "tok", "CLOUDFLARE_ACCOUNT_ID": ACCOUNT}
    assert json.loads(seen[-1].content) == {"name": "octocat-portfolio", "production_branch": "main"}


async def test_cloudflare_asks_which_account_when_the_token_sees_several() -> None:
    accounts = [{"id": ACCOUNT, "name": "Octo"}, {"id": "b" * 32, "name": "Work"}]
    routes: Routes = {
        ("GET", "https://api.cloudflare.com/client/v4/accounts"): httpx.Response(200, json={"result": accounts}),
    }
    with pytest.raises(PlatformAccountRequiredError) as err:
        await prepare(client(routes), PublishTarget.CLOUDFLARE_PAGES)
    assert err.value.details == {"accounts": accounts}

    projects = f"https://api.cloudflare.com/client/v4/accounts/{'b' * 32}/pages/projects"
    picked: Routes = {
        ("GET", f"{projects}/octocat-portfolio"): httpx.Response(200, json={"result": {"subdomain": "s.pages.dev"}})
    }
    project = await prepare(client(picked), PublishTarget.CLOUDFLARE_PAGES, account_id="b" * 32)
    assert project.secrets["CLOUDFLARE_ACCOUNT_ID"] == "b" * 32


async def test_cloudflare_names_the_permission_a_token_lacks() -> None:
    projects = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT}/pages/projects"
    routes: Routes = {("GET", f"{projects}/octocat-portfolio"): httpx.Response(403)}
    with pytest.raises(PlatformTokenInvalidError, match="Cloudflare Pages: Edit"):
        await prepare(client(routes), PublishTarget.CLOUDFLARE_PAGES, account_id=ACCOUNT)


async def test_netlify_creates_the_site_or_says_the_name_is_taken() -> None:
    user = ("GET", "https://api.netlify.com/api/v1/user")
    create = ("POST", "https://api.netlify.com/api/v1/sites")
    routes: Routes = {
        user: httpx.Response(200, json={}),
        create: httpx.Response(201, json={"id": "s1", "ssl_url": "https://octocat-portfolio.netlify.app"}),
    }
    project = await prepare(client(routes), PublishTarget.NETLIFY)
    assert project.site_url == "https://octocat-portfolio.netlify.app"
    assert project.secrets == {"NETLIFY_AUTH_TOKEN": "tok"}

    with pytest.raises(PlatformProjectTakenError):
        await prepare(client({user: httpx.Response(200, json={}), create: httpx.Response(422)}), PublishTarget.NETLIFY)


@pytest.mark.parametrize(
    "make", [lambda: httpx.Response(500), lambda: httpx.Response(429), lambda: httpx.Response(200, text="<html>")]
)
async def test_platform_failures(make: Callable[[], httpx.Response]) -> None:
    routes: Routes = {("GET", "https://api.vercel.com/v2/user"): make()}
    with pytest.raises(PlatformError):
        await prepare(client(routes), PublishTarget.VERCEL)


async def test_an_unreachable_platform() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    c = HttpDeployPlatformClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with pytest.raises(PlatformError):
        await prepare(c, PublishTarget.NETLIFY)
