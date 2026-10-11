"""The template catalog client against a mocked transport."""

import base64
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.clients.templates import (
    HttpTemplateCatalog,
    TemplateCache,
    TemplateNotFoundError,
    TemplateUnavailableError,
)
from app.constants.publish import MAX_TEMPLATE_BYTES

CATALOG_URL = "https://templates.example/catalog.json"
FILES_URL = "https://templates.example/dev-portfolio/paper/1/files.json"


def entry(**over: Any) -> dict[str, Any]:
    return {
        "id": "paper",
        "category": "dev-portfolio",
        "name": "Paper",
        "kind": "static",
        "version": 1,
        "contentPath": "content/portfolio.js",
        "thumbnailUrl": "https://templates.example/thumb.webp",
        "filesUrl": FILES_URL,
        **over,
    }


def catalog(
    handler: Callable[[httpx.Request], httpx.Response], cache: TemplateCache | None = None
) -> HttpTemplateCatalog:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpTemplateCatalog(http, CATALOG_URL, cache or TemplateCache())


async def test_reads_an_entry_and_its_files_and_caches_both() -> None:
    seen: list[str] = []
    files = {"index.html": "<main></main>", "binary": {"assets/me.png": base64.b64encode(b"png").decode()}}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if str(request.url) == CATALOG_URL:
            # A broken entry and a duplicate are dropped, like the SPA does.
            return httpx.Response(200, json={"templates": [{"id": "x"}, entry(), entry(version=9)]})
        return httpx.Response(200, json=files)

    client = catalog(handler)
    found = await client.get_template("paper")
    assert (found.kind, found.version, found.content_path) == ("static", 1, "content/portfolio.js")
    template = await client.get_files(found)
    assert template.text == {"index.html": "<main></main>"}
    assert template.binary == {"assets/me.png": b"png"}

    await client.get_template("paper")
    await client.get_files(found)
    assert seen == [CATALOG_URL, FILES_URL]
    with pytest.raises(TemplateNotFoundError):
        await client.get_template("missing")


@pytest.mark.parametrize(
    "files",
    [
        {".github/workflows/x.yml": "on: push"},  # makable's own place
        {"../escape": "x"},
        {"index.html": 1},
        {"binary": {"a.png": "not base64!"}},
        [],
    ],
)
async def test_refuses_template_files_it_cant_publish(files: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == CATALOG_URL:
            return httpx.Response(200, json={"templates": [entry()]})
        return httpx.Response(200, json=files)

    client = catalog(handler)
    with pytest.raises(TemplateUnavailableError):
        await client.get_files(await client.get_template("paper"))


async def test_only_reads_files_from_the_catalogs_host() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == CATALOG_URL
        return httpx.Response(200, json={"templates": [entry(filesUrl="https://elsewhere.example/files.json")]})

    client = catalog(handler)
    with pytest.raises(TemplateUnavailableError):
        await client.get_files(await client.get_template("paper"))


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, content=b"x" * (MAX_TEMPLATE_BYTES + 1)),
    ],
)
async def test_a_catalog_that_fails_is_unavailable(response: httpx.Response) -> None:
    client = catalog(lambda _: response)
    with pytest.raises(TemplateUnavailableError):
        await client.get_template("paper")


async def test_the_catalog_is_read_again_after_its_ttl() -> None:
    now = [0.0]
    calls: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, json={"templates": [entry()]})

    client = catalog(handler, TemplateCache(ttl=300, clock=lambda: now[0]))
    await client.get_template("paper")
    now[0] = 301
    await client.get_template("paper")
    assert len(calls) == 2
