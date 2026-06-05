"""
jobs_service.py -- SQLite-backed scan job tracker.

Persists scan state so the frontend can restore progress across page reloads.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import aiosqlite

from config import settings

logger = logging.getLogger(__name__)

# DB lives in the configurable data directory (DATA_DIR env / api_key.json)
def _db_path() -> str:
    p = Path(settings.data_dir)
    p.mkdir(parents=True, exist_ok=True)
    return str(p / "jobs.db")


_CREATE = """
CREATE TABLE IF NOT EXISTS scan_jobs (
    job_id        TEXT PRIMARY KEY,
    status        TEXT NOT NULL DEFAULT 'running',
    folder_path   TEXT NOT NULL,
    total_files   INTEGER NOT NULL DEFAULT 0,
    files_done    INTEGER NOT NULL DEFAULT 0,
    batch_size    INTEGER NOT NULL DEFAULT 25,
    current_batch INTEGER NOT NULL DEFAULT 0,
    total_batches INTEGER NOT NULL DEFAULT 0,
    start_time    REAL NOT NULL,
    end_time      REAL,
    overall_score INTEGER,
    total_issues  INTEGER,
    model_used    TEXT,
    report_path   TEXT,
    error_detail  TEXT,
    max_files     INTEGER NOT NULL DEFAULT -1,
    sleep_between_files INTEGER NOT NULL DEFAULT 30,
    context       TEXT,
    focus_areas   TEXT
)
"""

_CREATE_FILES = """
CREATE TABLE IF NOT EXISTS scan_job_files (
    job_id TEXT,
    filename TEXT,
    index_num INTEGER,
    batch_num INTEGER,
    round_num INTEGER,
    score INTEGER,
    issues INTEGER,
    error TEXT,
    delayed BOOLEAN,
    time_taken REAL,
    size_bytes INTEGER,
    timestamp TEXT,
    ts_ms REAL,
    PRIMARY KEY (job_id, filename)
)
"""

_tables_ready = False


async def _ensure_tables(db: aiosqlite.Connection) -> None:
    global _tables_ready
    if not _tables_ready:
        await db.execute(_CREATE)
        await db.execute(_CREATE_FILES)
        # Migrate existing DBs: add columns that were introduced after initial creation
        _migrations = [
            "ALTER TABLE scan_jobs ADD COLUMN batch_size    INTEGER NOT NULL DEFAULT 25",
            "ALTER TABLE scan_jobs ADD COLUMN current_batch INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE scan_jobs ADD COLUMN total_batches INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE scan_jobs ADD COLUMN max_files     INTEGER NOT NULL DEFAULT -1",
            "ALTER TABLE scan_jobs ADD COLUMN sleep_between_files INTEGER NOT NULL DEFAULT 30",
            "ALTER TABLE scan_jobs ADD COLUMN context       TEXT",
            "ALTER TABLE scan_jobs ADD COLUMN focus_areas   TEXT",
        ]
        for sql in _migrations:
            try:
                await db.execute(sql)
            except Exception:
                pass  # Column already exists — safe to ignore
        await db.commit()
        _tables_ready = True


from contextlib import asynccontextmanager

@asynccontextmanager
async def _open_db():
    """Open the jobs DB with WAL mode + busy timeout for concurrent access."""
    async with aiosqlite.connect(_db_path()) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        yield db


async def create_job(
    job_id: str,
    folder_path: str,
    max_files: int = -1,
    sleep_secs: int = 30,
    context: Optional[str] = None,
    focus_areas: Optional[str] = None,
) -> None:
    """Insert a new running job record."""
    async with _open_db() as db:
        await _ensure_tables(db)
        await db.execute(
            "INSERT INTO scan_jobs (job_id, status, folder_path, max_files, sleep_between_files, context, focus_areas, start_time) "
            "VALUES (?, 'running', ?, ?, ?, ?, ?, ?)",
            (job_id, folder_path, max_files, sleep_secs, context, focus_areas, time.time()),
        )
        await db.commit()


async def update_job(job_id: str, **kwargs) -> None:
    """Update fields on an existing job record."""
    if not kwargs:
        return
    sets = ", ".join(f"{k}=?" for k in kwargs)
    vals = list(kwargs.values()) + [job_id]
    try:
        async with _open_db() as db:
            await _ensure_tables(db)
            await db.execute(f"UPDATE scan_jobs SET {sets} WHERE job_id=?", vals)
            await db.commit()
    except Exception:
        logger.error("Failed to update job %s with %s", job_id, kwargs, exc_info=True)


async def get_latest_job() -> Optional[dict]:
    """Return the most recent job record, or None."""
    try:
        async with _open_db() as db:
            await _ensure_tables(db)
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM scan_jobs ORDER BY start_time DESC LIMIT 1"
            ) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None
    except Exception:
        logger.error("Failed to fetch latest job", exc_info=True)
        return None

async def get_job(job_id: str) -> Optional[dict]:
    """Return a specific job record by ID."""
    try:
        async with _open_db() as db:
            await _ensure_tables(db)
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM scan_jobs WHERE job_id=?", (job_id,)) as cur:
                row = await cur.fetchone()
        return dict(row) if row else None
    except Exception:
        logger.error("Failed to fetch job %s", job_id, exc_info=True)
        return None

async def log_file_done(job_id: str, file_data: dict) -> None:
    """Insert or update a completed file record for a job."""
    try:
        async with _open_db() as db:
            await _ensure_tables(db)
            await db.execute(
                """
                INSERT INTO scan_job_files 
                (job_id, filename, index_num, batch_num, round_num, score, issues, error, delayed, time_taken, size_bytes, timestamp, ts_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, filename) DO UPDATE SET
                    index_num=excluded.index_num,
                    batch_num=excluded.batch_num,
                    round_num=excluded.round_num,
                    score=excluded.score,
                    issues=excluded.issues,
                    error=excluded.error,
                    delayed=excluded.delayed,
                    time_taken=excluded.time_taken,
                    size_bytes=excluded.size_bytes,
                    timestamp=excluded.timestamp,
                    ts_ms=excluded.ts_ms
                """,
                (
                    job_id,
                    file_data.get("filename"),
                    file_data.get("index"),
                    file_data.get("batch"),
                    file_data.get("round", 1),
                    file_data.get("score"),
                    file_data.get("issues", 0),
                    file_data.get("error"),
                    file_data.get("delayed", False),
                    file_data.get("time_taken"),
                    file_data.get("size_bytes"),
                    file_data.get("timestamp"),
                    file_data.get("tsMs")
                )
            )
            await db.commit()
    except Exception:
        logger.error("Failed to log file done for %s", job_id, exc_info=True)


async def get_job_files(job_id: str) -> list[dict]:
    """Retrieve all logged file completions for a job."""
    try:
        async with _open_db() as db:
            await _ensure_tables(db)
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM scan_job_files WHERE job_id=? ORDER BY ts_ms ASC", (job_id,)) as cur:
                rows = await cur.fetchall()
                return [dict(r) for r in rows]
    except Exception:
        logger.error("Failed to fetch files for job %s", job_id, exc_info=True)
        return []

# --- END OF FILE: jobs_service.py ---
