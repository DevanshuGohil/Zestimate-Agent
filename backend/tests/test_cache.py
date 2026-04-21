"""SQLite cache tests — fully offline, using a tmp_path fixture."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from zestimate_agent.cache import Cache, CacheLookup
from zestimate_agent.models import Confidence, ZestimateResult


def _result(zestimate: int = 500_000, zpid: str = "12345") -> ZestimateResult:
    return ZestimateResult(
        address="101 Lombard St, San Francisco, CA 94111",
        zestimate=zestimate,
        zpid=zpid,
        fetched_at=datetime.now(tz=timezone.utc),
        provider_used="stub",
        confidence=Confidence.HIGH,
    )


def _cache(tmp_path: Path, ttl_hours: int = 24, failure_ttl_hours: int = 6) -> Cache:
    return Cache(tmp_path / "test_cache.db", ttl_hours=ttl_hours, failure_ttl_hours=failure_ttl_hours)


# ---------------------------------------------------------------------------
# make_key (sync — pure)
# ---------------------------------------------------------------------------


def test_make_key_is_stable() -> None:
    k1 = Cache.make_key("101 LOMBARD ST, SAN FRANCISCO, CA 94111")
    k2 = Cache.make_key("101 LOMBARD ST, SAN FRANCISCO, CA 94111")
    assert k1 == k2
    assert len(k1) == 64  # sha256 hex


def test_make_key_uppercases() -> None:
    k1 = Cache.make_key("101 lombard st")
    k2 = Cache.make_key("101 LOMBARD ST")
    assert k1 == k2


def test_make_key_different_addresses_differ() -> None:
    k1 = Cache.make_key("101 LOMBARD ST, SAN FRANCISCO, CA 94111")
    k2 = Cache.make_key("101 FILBERT ST, SAN FRANCISCO, CA 94133")
    assert k1 != k2


# ---------------------------------------------------------------------------
# Store and lookup — success
# ---------------------------------------------------------------------------


async def test_store_and_lookup_returns_result(tmp_path: Path) -> None:
    c = _cache(tmp_path)
    key = "abc"
    result = _result()
    await c.store(key, result)

    hit = await c.lookup(key)
    assert hit.hit is True
    assert hit.was_failure is False
    assert hit.result is not None
    assert hit.result.zestimate == 500_000
    assert hit.result.zpid == "12345"


async def test_lookup_miss_returns_not_hit(tmp_path: Path) -> None:
    c = _cache(tmp_path)
    hit = await c.lookup("nonexistent-key")
    assert hit.hit is False
    assert hit.result is None


async def test_store_replaces_existing(tmp_path: Path) -> None:
    c = _cache(tmp_path)
    key = "x"
    await c.store(key, _result(zestimate=100_000))
    await c.store(key, _result(zestimate=200_000))
    hit = await c.lookup(key)
    assert hit.result is not None
    assert hit.result.zestimate == 200_000


# ---------------------------------------------------------------------------
# TTL expiry
# ---------------------------------------------------------------------------


async def test_expired_result_returns_miss(tmp_path: Path) -> None:
    c = _cache(tmp_path, ttl_hours=0)  # ttl = 0 hours → expires immediately
    key = "exp"
    await c.store(key, _result())
    hit = await c.lookup(key)
    assert hit.hit is False


async def test_unexpired_result_returns_hit(tmp_path: Path) -> None:
    c = _cache(tmp_path, ttl_hours=24)
    key = "fresh"
    await c.store(key, _result())
    hit = await c.lookup(key)
    assert hit.hit is True


# ---------------------------------------------------------------------------
# Failure records
# ---------------------------------------------------------------------------


async def test_store_failure_and_lookup_returns_failure(tmp_path: Path) -> None:
    c = _cache(tmp_path)
    key = "fail"
    await c.store_failure(key)
    hit = await c.lookup(key)
    assert hit.hit is True
    assert hit.was_failure is True
    assert hit.result is None


async def test_expired_failure_returns_miss(tmp_path: Path) -> None:
    c = _cache(tmp_path, failure_ttl_hours=0)
    key = "efail"
    await c.store_failure(key)
    hit = await c.lookup(key)
    assert hit.hit is False


# ---------------------------------------------------------------------------
# Clear
# ---------------------------------------------------------------------------


async def test_clear_empties_all_entries(tmp_path: Path) -> None:
    c = _cache(tmp_path)
    await c.store("a", _result())
    await c.store_failure("b")
    await c.clear()
    assert (await c.lookup("a")).hit is False
    assert (await c.lookup("b")).hit is False


# ---------------------------------------------------------------------------
# Persistence across instances
# ---------------------------------------------------------------------------


async def test_data_persists_across_instances(tmp_path: Path) -> None:
    db = tmp_path / "shared.db"
    c1 = Cache(db)
    await c1.store("persistent", _result(zestimate=777_000))

    c2 = Cache(db)
    hit = await c2.lookup("persistent")
    assert hit.hit is True
    assert hit.result is not None
    assert hit.result.zestimate == 777_000


# ---------------------------------------------------------------------------
# WAL mode
# ---------------------------------------------------------------------------


async def test_wal_mode_enabled(tmp_path: Path) -> None:
    import aiosqlite

    c = _cache(tmp_path)
    await c.store("wal_check", _result())  # triggers _connection which sets WAL

    async with aiosqlite.connect(tmp_path / "test_cache.db") as db:
        async with db.execute("PRAGMA journal_mode") as cursor:
            row = await cursor.fetchone()
    assert row is not None
    assert row[0] == "wal"


# ---------------------------------------------------------------------------
# Eviction
# ---------------------------------------------------------------------------


async def test_evict_expired_removes_stale_rows(tmp_path: Path) -> None:
    c = _cache(tmp_path, ttl_hours=0, failure_ttl_hours=0)
    await c.store("stale", _result())
    await c.store_failure("stale_fail")

    count = await c.evict_expired()
    assert count == 2
    assert (await c.lookup("stale")).hit is False
    assert (await c.lookup("stale_fail")).hit is False


async def test_evict_does_not_remove_fresh_rows(tmp_path: Path) -> None:
    c = _cache(tmp_path, ttl_hours=24, failure_ttl_hours=6)
    await c.store("fresh", _result())
    count = await c.evict_expired()
    assert count == 0
    assert (await c.lookup("fresh")).hit is True
