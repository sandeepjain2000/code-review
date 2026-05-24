"""
cache_service.py -- SQLite-backed response cache.

Caches review results by a hash of (filename + code content + model).
Respects TTL from config. Uses aiosqlite for non-blocking I/O.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Optional
from contextlib import asynccontextmanager

import aiosqlite

from config import settings

_CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS review_cache (
        cache_key   TEXT PRIMARY KEY,
        result_json TEXT NOT NULL,
        created_at  REAL NOT NULL
    )
"""


class CacheService:
    """Async SQLite cache for review results."""

    def __init__(self, db_path: str, ttl_seconds: int):
        self._db_path = db_path
        self._ttl = ttl_seconds
        self._enabled = settings.cache.enabled
        self._tables_ready = False

    @asynccontextmanager
    async def _open(self):
        """Open DB with WAL mode + busy timeout for concurrent access."""
        async with aiosqlite.connect(self._db_path) as db:
            await db.execute("PRAGMA journal_mode=WAL")
            await db.execute("PRAGMA busy_timeout=5000")
            yield db

    async def _ensure_tables(self, db: aiosqlite.Connection) -> None:
        if not self._tables_ready:
            await db.execute(_CREATE_TABLE)
            self._tables_ready = True

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get(self, cache_key: str) -> Optional[dict]:
        """Return cached result dict, or None if missing/expired."""
        if not self._enabled:
            return None

        async with self._open() as db:
            await self._ensure_tables(db)
            async with db.execute(
                "SELECT result_json, created_at FROM review_cache WHERE cache_key = ?",
                (cache_key,),
            ) as cursor:
                row = await cursor.fetchone()

        if row is None:
            return None

        result_json, created_at = row
        if time.time() - created_at > self._ttl:
            await self.delete(cache_key)
            return None

        return json.loads(result_json)

    async def set(self, cache_key: str, result: dict) -> None:
        """Persist result under cache_key."""
        if not self._enabled:
            return

        async with self._open() as db:
            await self._ensure_tables(db)
            await db.execute(
                """
                INSERT INTO review_cache (cache_key, result_json, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET
                    result_json = excluded.result_json,
                    created_at  = excluded.created_at
                """,
                (cache_key, json.dumps(result), time.time()),
            )
            await db.commit()

    async def delete(self, cache_key: str) -> None:
        async with self._open() as db:
            await self._ensure_tables(db)
            await db.execute(
                "DELETE FROM review_cache WHERE cache_key = ?", (cache_key,)
            )
            await db.commit()

    async def purge_expired(self) -> int:
        """Delete all expired entries. Returns count deleted."""
        cutoff = time.time() - self._ttl
        async with self._open() as db:
            await self._ensure_tables(db)
            cursor = await db.execute(
                "DELETE FROM review_cache WHERE created_at < ?", (cutoff,)
            )
            await db.commit()
            return cursor.rowcount

    async def stats(self) -> dict:
        async with self._open() as db:
            await self._ensure_tables(db)
            async with db.execute("SELECT COUNT(*) FROM review_cache") as c:
                total = (await c.fetchone())[0]
            async with db.execute(
                "SELECT COUNT(*) FROM review_cache WHERE created_at >= ?",
                (time.time() - self._ttl,),
            ) as c:
                valid = (await c.fetchone())[0]

        return {"total_entries": total, "valid_entries": valid, "expired_entries": total - valid}

    # ------------------------------------------------------------------
    # Key generation
    # ------------------------------------------------------------------

    @staticmethod
    def make_key(filename: str, code: str, model: str, context: str = "") -> str:
        """
        Include an optional context (e.g. repo path or folder root) so
        identically-named files from different repos never collide.
        """
        payload = f"{context}::{filename}::{model}::{code}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def make_pr_key(owner: str, repo: str, pull_number: int, model: str) -> str:
        payload = f"pr::{owner}::{repo}::{pull_number}::{model}"
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_cache: CacheService | None = None


def get_cache() -> CacheService:
    global _cache
    if _cache is None:
        from pathlib import Path
        _cache = CacheService(
            db_path=str(Path(settings.data_dir) / "cache.db"),
            ttl_seconds=settings.cache.ttl_seconds,
        )
    return _cache

# --- END OF FILE: cache_service.py ---
