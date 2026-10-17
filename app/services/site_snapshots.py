"""Sites' files at a commit, kept in memory. A commit never changes, so a snapshot is good until it's dropped for
space or age: AI edits and Undo read the same commit several times (the context, the dry run, the commit), and the
browser's reload reads the commit the last save made."""

import asyncio
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from app.constants.site import SNAPSHOT_CACHE_BYTES, SNAPSHOT_TTL
from app.schemas.site import SiteSnapshot


def snapshot_from_archive(commit_sha: str, archive: dict[str, bytes]) -> SiteSnapshot:
    """Sorts a repo archive into text files (UTF-8) and the others."""
    files: dict[str, str] = {}
    binary: list[str] = []
    for path, data in sorted(archive.items()):
        try:
            files[path] = data.decode()
        except UnicodeDecodeError:
            binary.append(path)
    return SiteSnapshot(commit_sha=commit_sha, files=files, binary=binary)


class SiteSnapshots:
    """Snapshots by (repo, commit), the least recently used dropped first past `max_bytes`, and each for at most
    `ttl` seconds (a repo can be deleted and made again with the same name and commits... rarely, but a stale entry
    shouldn't live forever). Concurrent reads of a snapshot that isn't cached share one download. Process-wide: each
    worker keeps its own."""

    def __init__(
        self,
        ttl: float = SNAPSHOT_TTL,
        max_bytes: int = SNAPSHOT_CACHE_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._ttl = ttl
        self._max_bytes = max_bytes
        self._clock = clock
        self._bytes = 0
        self._entries: OrderedDict[tuple[str, str], tuple[SiteSnapshot, int, float]] = OrderedDict()
        self._loads: dict[tuple[str, str], asyncio.Future[SiteSnapshot]] = {}

    async def get(self, full_name: str, commit_sha: str, load: Callable[[], Awaitable[SiteSnapshot]]) -> SiteSnapshot:
        """The snapshot, from the cache or `load`. The download runs on its own, shared by concurrent callers, so a
        caller that's cancelled (a closed request) doesn't cancel it for the others. `load`'s errors reach every
        caller waiting for it, and nothing is cached."""
        key = (full_name, commit_sha)
        entry = self._entries.get(key)
        if entry is not None and entry[2] > self._clock():
            self._entries.move_to_end(key)
            return entry[0]
        if entry is not None:
            self._drop(key)
        task = self._loads.get(key)
        if task is None:
            task = asyncio.ensure_future(self._load(key, full_name, load))
            # Read the outcome even if every caller went away, so a failure isn't logged as never retrieved.
            task.add_done_callback(lambda t: None if t.cancelled() else t.exception())
            self._loads[key] = task
        return await asyncio.shield(task)

    async def _load(
        self, key: tuple[str, str], full_name: str, load: Callable[[], Awaitable[SiteSnapshot]]
    ) -> SiteSnapshot:
        try:
            snapshot = await load()
            self.put(full_name, snapshot)
            return snapshot
        finally:
            self._loads.pop(key, None)

    def peek(self, full_name: str, commit_sha: str) -> SiteSnapshot | None:
        """The snapshot if it's cached (it counts as used), without loading it."""
        key = (full_name, commit_sha)
        entry = self._entries.get(key)
        if entry is None or entry[2] <= self._clock():
            return None
        self._entries.move_to_end(key)
        return entry[0]

    def put(self, full_name: str, snapshot: SiteSnapshot) -> None:
        """Caches a snapshot the caller already has (e.g. the files of a commit it just made)."""
        key = (full_name, snapshot.commit_sha)
        if key in self._entries:
            self._drop(key)
        size = sum(len(text) for text in snapshot.files.values())
        if size > self._max_bytes:
            return
        self._entries[key] = (snapshot, size, self._clock() + self._ttl)
        self._bytes += size
        while self._bytes > self._max_bytes:
            self._drop(next(iter(self._entries)))

    def clear(self) -> None:
        self._entries.clear()
        self._bytes = 0

    def _drop(self, key: tuple[str, str]) -> None:
        _, size, _ = self._entries.pop(key)
        self._bytes -= size


SITE_SNAPSHOTS = SiteSnapshots()
