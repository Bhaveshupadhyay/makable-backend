"""The published template catalog: which templates exist, and each one's files. The same catalog the SPA's preview
loads (`@makable/shared` `template-catalog.ts`), so a published site is the one the user saw."""

import asyncio
import base64
import binascii
import json
import time
from collections import OrderedDict
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from fastapi import status
from pydantic import ValidationError

from app.constants.ai_edit import SAFE_REPO_PATH
from app.constants.publish import (
    CATALOG_TTL,
    MAX_CATALOG_BYTES,
    MAX_TEMPLATE_BYTES,
    MAX_TEMPLATE_FILE_BYTES,
    RESERVED_PATH,
    TEMPLATE_FILES_CACHE_BYTES,
)
from app.core.exceptions import AppError, ExternalServiceError
from app.core.limits import ServiceBusyError
from app.schemas.template import TemplateEntry, TemplateFiles


class TemplateUnavailableError(ExternalServiceError):
    code = "template_unavailable"
    message = "The template couldn't be loaded. Try again shortly."


class TemplateNotFoundError(AppError):
    status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    code = "template_not_found"
    message = "That template isn't in the catalog any more."


class TemplateCatalogClient(Protocol):
    async def get_template(self, template_id: str) -> TemplateEntry: ...

    async def get_files(self, entry: TemplateEntry) -> TemplateFiles: ...


class TemplateCache:
    """The catalog for `CATALOG_TTL` seconds, and the files of recently used templates (their URLs carry the
    template's version, so they never change), up to `max_bytes` in total. Process-wide: each worker keeps its
    own. `loading` is held while a template's files are downloaded and parsed, so requests for a template that
    isn't cached yet share one download, and a worker never holds more than one template outside the cache."""

    def __init__(
        self,
        ttl: float = CATALOG_TTL,
        clock: Callable[[], float] = time.monotonic,
        max_bytes: int = TEMPLATE_FILES_CACHE_BYTES,
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._max_bytes = max_bytes
        self._bytes = 0
        self.catalog: tuple[dict[str, TemplateEntry], float] | None = None
        self.files: OrderedDict[str, TemplateFiles] = OrderedDict()
        self.loading = asyncio.Lock()

    def get_catalog(self) -> dict[str, TemplateEntry] | None:
        if self.catalog is None or self.catalog[1] <= self._clock():
            return None
        return self.catalog[0]

    def put_catalog(self, entries: dict[str, TemplateEntry]) -> None:
        self.catalog = (entries, self._clock() + self._ttl)

    def get_files(self, url: str) -> TemplateFiles | None:
        files = self.files.get(url)
        if files is not None:
            self.files.move_to_end(url)
        return files

    def put_files(self, url: str, files: TemplateFiles) -> None:
        old = self.files.pop(url, None)
        if old is not None:
            self._bytes -= _size(old)
        self.files[url] = files
        self._bytes += _size(files)
        # The one just added stays, even when it's bigger than the budget on its own.
        while self._bytes > self._max_bytes and len(self.files) > 1:
            _, dropped = self.files.popitem(last=False)
            self._bytes -= _size(dropped)

    def clear(self) -> None:
        self.catalog = None
        self.files.clear()
        self._bytes = 0
        self.loading = asyncio.Lock()


TEMPLATE_CACHE = TemplateCache()


class HttpTemplateCatalog:
    """Reads the catalog at `catalog_url` over the shared `httpx.AsyncClient`. Template files are only read from
    the catalog's own host, so a bad catalog entry can't make the server fetch anything else."""

    def __init__(self, http: httpx.AsyncClient, catalog_url: str, cache: TemplateCache = TEMPLATE_CACHE) -> None:
        self._http = http
        self._catalog_url = catalog_url
        self._cache = cache

    async def get_template(self, template_id: str) -> TemplateEntry:
        """The catalog's entry for a template (its latest version).

        Raises:
            TemplateNotFoundError: The catalog has no such template.
            TemplateUnavailableError: The catalog couldn't be read.
        """
        entries = self._cache.get_catalog()
        if entries is None:
            entries = _parse_catalog(await self._get(self._catalog_url, MAX_CATALOG_BYTES, "template catalog"))
            self._cache.put_catalog(entries)
        entry = entries.get(template_id)
        if entry is None:
            raise TemplateNotFoundError()
        return entry

    async def get_files(self, entry: TemplateEntry) -> TemplateFiles:
        """A template's files from its `files.json`.

        Raises:
            TemplateUnavailableError: The files couldn't be read, or are invalid.
        """
        if not _same_origin(entry.files_url, self._catalog_url):
            raise TemplateUnavailableError("The template's files aren't hosted with the catalog")
        files = self._cache.get_files(entry.files_url)
        if files is not None:
            return files
        async with self._cache.loading:
            # Another request may have loaded it while this one waited.
            files = self._cache.get_files(entry.files_url)
            if files is None:
                files = _parse_files(await self._get(entry.files_url, MAX_TEMPLATE_BYTES, "template"))
                self._cache.put_files(entry.files_url, files)
        return files

    async def _get(self, url: str, max_bytes: int, what: str) -> Any:
        try:
            async with self._http.stream("GET", url, headers={"Accept": "application/json"}) as res:
                if res.status_code != status.HTTP_200_OK:
                    raise TemplateUnavailableError(f"The template catalog answered HTTP {res.status_code}")
                body = bytearray()
                async for chunk in res.aiter_bytes():
                    body += chunk
                    if len(body) > max_bytes:
                        raise TemplateUnavailableError(f"The {what} is too large")
        except httpx.PoolTimeout as err:
            raise ServiceBusyError() from err
        except httpx.HTTPError as err:
            raise TemplateUnavailableError() from err
        try:
            return json.loads(body)
        except ValueError as err:
            raise TemplateUnavailableError("The template catalog isn't valid JSON") from err


def _parse_catalog(data: Any) -> dict[str, TemplateEntry]:
    """The catalog's templates by id. Entries that don't parse, or repeat an id, are dropped, like the SPA does."""
    items = data.get("templates") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise TemplateUnavailableError("The template catalog is invalid")
    entries: dict[str, TemplateEntry] = {}
    for item in items:
        try:
            entry = TemplateEntry.model_validate(item)
        except ValidationError:
            continue
        entries.setdefault(entry.id, entry)
    return entries


def _parse_files(data: Any) -> TemplateFiles:
    """`{"<path>": "<text>", ..., "binary": {"<path>": "<base64>"}}`, with every path checked."""
    if not isinstance(data, dict):
        raise TemplateUnavailableError("The template's files are invalid")
    binary_data = data.get("binary", {})
    text = {path: content for path, content in data.items() if path != "binary"}
    if not isinstance(binary_data, dict):
        raise TemplateUnavailableError("The template's files are invalid")
    binary: dict[str, bytes] = {}
    for path, content in binary_data.items():
        try:
            binary[path] = base64.b64decode(content, validate=True)
        except (binascii.Error, TypeError, ValueError) as err:
            raise TemplateUnavailableError("The template's files are invalid") from err
        if len(binary[path]) > MAX_TEMPLATE_FILE_BYTES:
            raise TemplateUnavailableError(f"The template has a file too large to publish: {path}")
    if not all(isinstance(content, str) for content in text.values()):
        raise TemplateUnavailableError("The template's files are invalid")
    for path in (*text, *binary):
        if not SAFE_REPO_PATH.fullmatch(path) or RESERVED_PATH.match(path):
            raise TemplateUnavailableError(f"The template has a file makable can't publish: {path}")
    return TemplateFiles(text=text, binary=binary)


def _size(files: TemplateFiles) -> int:
    return sum(len(content) for content in files.text.values()) + sum(len(data) for data in files.binary.values())


def _same_origin(url: str, other: str) -> bool:
    a, b = urlsplit(url), urlsplit(other)
    return a.scheme == "https" and (a.scheme, a.netloc) == (b.scheme, b.netloc)
