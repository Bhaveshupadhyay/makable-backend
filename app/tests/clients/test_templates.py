"""The template catalog client against a mocked transport."""

import asyncio
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
from app.constants.publish import MAX_CATALOG_BYTES, MAX_TEMPLATE_BYTES
from app.schemas.template import TemplateFiles

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
        httpx.Response(200, content=b"x" * (MAX_CATALOG_BYTES + 1)),
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


def files_handler(files: Any, seen: list[str] | None = None) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == CATALOG_URL:
            return httpx.Response(200, json={"templates": [entry()]})
        if seen is not None:
            seen.append(str(request.url))
        return files if isinstance(files, httpx.Response) else httpx.Response(200, json=files)

    return handler


async def test_template_files_have_their_own_larger_limit() -> None:
    # Past the catalog's limit, inside the files' one.
    big = {"main.js": "x" * (MAX_CATALOG_BYTES + 1)}
    client = catalog(files_handler(big))
    assert (await client.get_files(await client.get_template("paper"))).text == big

    client = catalog(files_handler(httpx.Response(200, content=b"x" * (MAX_TEMPLATE_BYTES + 1))))
    with pytest.raises(TemplateUnavailableError, match="too large"):
        await client.get_files(await client.get_template("paper"))


async def test_refuses_a_binary_file_too_large_to_send_to_github(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.clients.templates.MAX_TEMPLATE_FILE_BYTES", 3)
    client = catalog(files_handler({"binary": {"ok.png": base64.b64encode(b"123").decode()}}))
    assert (await client.get_files(await client.get_template("paper"))).binary == {"ok.png": b"123"}

    client = catalog(files_handler({"binary": {"video/big.webm": base64.b64encode(b"1234").decode()}}))
    with pytest.raises(TemplateUnavailableError, match=r"too large to publish: video/big\.webm"):
        await client.get_files(await client.get_template("paper"))


def test_the_cache_keeps_files_up_to_a_total_size_and_drops_the_least_recently_used() -> None:
    cache = TemplateCache(max_bytes=10)

    def files(text: int, binary: int = 0) -> TemplateFiles:
        return TemplateFiles(text={"a.js": "x" * text}, binary={"a.png": b"x" * binary})

    cache.put_files("a", files(2, 2))
    cache.put_files("b", files(4))
    assert cache.get_files("a") is not None  # now the most recently used
    cache.put_files("c", files(3))
    assert list(cache.files) == ["a", "c"]
    # Replacing an entry counts its new size, not both.
    cache.put_files("a", files(7))
    assert list(cache.files) == ["c", "a"]
    # One bigger than the whole budget is still kept, alone.
    cache.put_files("d", files(50))
    assert list(cache.files) == ["d"]
    cache.put_files("e", files(1))
    assert list(cache.files) == ["e"]


async def test_requests_for_an_uncached_template_share_one_download() -> None:
    seen: list[str] = []
    client = catalog(files_handler({"index.html": "<main></main>"}, seen))
    found = await client.get_template("paper")
    results = await asyncio.gather(*(client.get_files(found) for _ in range(5)))
    assert seen == [FILES_URL]
    assert all(r is results[0] for r in results)


async def test_a_failed_download_doesnt_block_the_next_one() -> None:
    answers = [httpx.Response(500), httpx.Response(200, json={"index.html": "ok"})]
    cache = TemplateCache()

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == CATALOG_URL:
            return httpx.Response(200, json={"templates": [entry()]})
        return answers.pop(0)

    client = catalog(handler, cache)
    found = await client.get_template("paper")
    with pytest.raises(TemplateUnavailableError):
        await client.get_files(found)
    assert (await client.get_files(found)).text == {"index.html": "ok"}
