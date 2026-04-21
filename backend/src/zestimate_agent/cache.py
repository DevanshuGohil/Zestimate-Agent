"""SQLite-backed TTL cache for ZestimateResult values.

Schema: one table with key (sha256 of normalized address), the serialised
result, a failure flag, and a timestamp.

- Successful results expire after `cache_ttl_hours` (default 24 h).
- Cached failures (address not found in Zillow) expire after
  `cache_failure_ttl_hours` (default 6 h), preventing hammering a
  provider for an address it will never resolve.
"""

from __future__ import annotations

import hashlib
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncGenerator

import aiosqlite
import structlog

from .models import ZestimateResult

log = structlog.get_logger(__name__)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS cache (
    key         TEXT    PRIMARY KEY,
    value       TEXT    NOT NULL,
    is_failure  INTEGER NOT NULL DEFAULT 0,
    cached_at   TEXT    NOT NULL
)
"""

_ENABLE_WAL = "PRAGMA journal_mode=WAL"
_EVICT_SQL = "DELETE FROM cache WHERE cached_at < ?"


class CachedFailure(Exception):
    """Raised by Cache.get() when an unexpired failure record exists for the key."""


@dataclass
class CacheLookup:
    """Return value of Cache.lookup(). Separates hit/miss from success/failure."""

    hit: bool
    result: ZestimateResult | None
    was_failure: bool = False


class Cache:
    def __init__(
        self,
        db_path: Path,
        ttl_hours: int = 24,
        failure_ttl_hours: int = 6,
    ) -> None:
        self._db_path = db_path
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ttl = timedelta(hours=ttl_hours)
        self._failure_ttl = timedelta(hours=failure_ttl_hours)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @staticmethod
    def make_key(address_canonical: str) -> str:
        """Stable SHA-256 key from a canonical address string."""
        return hashlib.sha256(address_canonical.upper().encode()).hexdigest()

    async def lookup(self, key: str) -> CacheLookup:
        """Return a CacheLookup describing what the cache knows about key."""
        async with self._connection() as db:
            async with db.execute(
                "SELECT value, is_failure, cached_at FROM cache WHERE key = ?",
                (key,),
            ) as cursor:
                row = await cursor.fetchone()

        if row is None:
            return CacheLookup(hit=False, result=None)

        value_json, is_failure, cached_at_str = row
        cached_at = datetime.fromisoformat(cached_at_str)
        ttl = self._failure_ttl if is_failure else self._ttl
        age = datetime.now(tz=timezone.utc) - cached_at

        if age > ttl:
            log.debug("cache.expired", key=key[:8], age_h=round(age.total_seconds() / 3600, 1))
            return CacheLookup(hit=False, result=None)

        if is_failure:
            log.debug("cache.hit.failure", key=key[:8])
            return CacheLookup(hit=True, result=None, was_failure=True)

        result = ZestimateResult.model_validate_json(value_json)
        log.debug("cache.hit", key=key[:8], zestimate=result.zestimate)
        return CacheLookup(hit=True, result=result)

    async def store(self, key: str, result: ZestimateResult) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        async with self._connection() as db:
            await db.execute(
                "INSERT OR REPLACE INTO cache (key, value, is_failure, cached_at) "
                "VALUES (?, ?, 0, ?)",
                (key, result.model_dump_json(), now),
            )
        log.debug("cache.stored", key=key[:8], zestimate=result.zestimate)

    async def store_failure(self, key: str) -> None:
        now = datetime.now(tz=timezone.utc).isoformat()
        async with self._connection() as db:
            await db.execute(
                "INSERT OR REPLACE INTO cache (key, value, is_failure, cached_at) "
                "VALUES (?, ?, 1, ?)",
                (key, "{}", now),
            )
        log.debug("cache.stored_failure", key=key[:8])

    async def clear(self) -> None:
        async with self._connection() as db:
            await db.execute("DELETE FROM cache")
        log.debug("cache.cleared")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def evict_expired(self) -> int:
        """Delete expired rows and return the count removed."""
        max_ttl = max(self._ttl, self._failure_ttl)
        cutoff = (datetime.now(tz=timezone.utc) - max_ttl).isoformat()
        async with self._connection() as db:
            cursor = await db.execute(_EVICT_SQL, (cutoff,))
            count = cursor.rowcount
        if count:
            log.debug("cache.evicted", count=count)
        return count

    @asynccontextmanager
    async def _connection(self) -> AsyncGenerator[aiosqlite.Connection, None]:
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute(_CREATE_SQL)
            await db.execute(_ENABLE_WAL)
            await db.commit()
            yield db
            await db.commit()
