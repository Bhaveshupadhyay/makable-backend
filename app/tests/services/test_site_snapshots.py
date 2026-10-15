import asyncio

import pytest

from app.schemas.site import SiteSnapshot
from app.services.site_snapshots import SiteSnapshots, snapshot_from_archive


def snapshot(sha: str, text: str = "x") -> SiteSnapshot:
    return SiteSnapshot(commit_sha=sha, files={"a.txt": text}, binary=[])


def test_an_archive_is_sorted_into_text_files_and_the_others() -> None:
    result = snapshot_from_archive("c" * 40, {"b.txt": "héllo".encode(), "a.png": b"\x89PNG\xff\xfe"})

    assert result == SiteSnapshot(commit_sha="c" * 40, files={"b.txt": "héllo"}, binary=["a.png"])


async def test_concurrent_reads_share_one_download_and_later_ones_hit_the_cache() -> None:
    cache = SiteSnapshots()
    loads = 0
    gate = asyncio.Event()

    async def load() -> SiteSnapshot:
        nonlocal loads
        loads += 1
        await gate.wait()
        return snapshot("a")

    first = asyncio.ensure_future(cache.get("o/r", "a", load))
    second = asyncio.ensure_future(cache.get("o/r", "a", load))
    await asyncio.sleep(0)
    gate.set()

    assert (await first, await second) == (snapshot("a"), snapshot("a"))
    assert await cache.get("o/r", "a", load) == snapshot("a")
    assert loads == 1


async def test_a_cancelled_reader_doesnt_cancel_the_download_for_the_others() -> None:
    cache = SiteSnapshots()
    gate = asyncio.Event()

    async def load() -> SiteSnapshot:
        await gate.wait()
        return snapshot("a")

    first = asyncio.ensure_future(cache.get("o/r", "a", load))
    second = asyncio.ensure_future(cache.get("o/r", "a", load))
    await asyncio.sleep(0)
    first.cancel()
    gate.set()

    assert await second == snapshot("a")
    with pytest.raises(asyncio.CancelledError):
        await first


async def test_a_failed_download_isnt_cached() -> None:
    cache = SiteSnapshots()

    async def fail() -> SiteSnapshot:
        raise RuntimeError("GitHub is down")

    async def load() -> SiteSnapshot:
        return snapshot("a")

    with pytest.raises(RuntimeError):
        await cache.get("o/r", "a", fail)
    assert await cache.get("o/r", "a", load) == snapshot("a")


async def test_the_least_recently_used_go_first_and_old_entries_expire() -> None:
    now = [0.0]
    cache = SiteSnapshots(ttl=60, max_bytes=10, clock=lambda: now[0])
    cache.put("o/r", snapshot("a", "xxxx"))
    cache.put("o/r", snapshot("b", "xxxx"))
    assert cache.peek("o/r", "a") is not None  # Used: "b" is now the oldest.
    cache.put("o/r", snapshot("c", "xxxx"))

    assert cache.peek("o/r", "b") is None
    assert cache.peek("o/r", "a") is not None
    now[0] = 61
    assert cache.peek("o/r", "a") is None
