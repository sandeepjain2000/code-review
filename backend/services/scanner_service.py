"""
scanner_service.py -- Walks a folder tree and returns reviewable source files.

Respects config exclusions (node_modules, .git, etc.)
and the allowed extension list.
"""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional

from config import settings
from utils.chunking import should_skip_file

# Shared thread pool for blocking file I/O -- keeps the event loop free
_io_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="scanner-io")

logger = logging.getLogger(__name__)


@dataclass
class SourceFile:
    """A single file discovered during a folder scan."""
    path: Path
    relative_path: str      # Relative to the scan root
    size_bytes: int
    content: str            # Decoded text content
    language: Optional[str] = None


class ScannerService:
    """Scans a folder tree and yields reviewable source files."""

    def __init__(self):
        cfg = settings.scanner
        self._excluded_dirs = set(cfg.excluded_dirs)
        self._included_exts = set(cfg.included_extensions)
        self._max_size_kb = cfg.max_file_size_kb

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def scan(
        self,
        folder_path: str | Path,
        recursive: bool = True,
        max_files: int = 50,
    ) -> List[SourceFile]:
        """
        Walk folder_path and return up to max_files source files.
        Files are sorted by path for deterministic ordering.
        Runs synchronously; use scan_async() from async callers.
        """
        root = Path(folder_path).resolve()

        if not root.exists():
            raise FileNotFoundError(f"Folder not found: {root}")
        if not root.is_dir():
            raise NotADirectoryError(f"Path is not a directory: {root}")

        found: List[SourceFile] = []

        for sf in self._walk(root, recursive):
            found.append(sf)
            if len(found) >= max_files:
                logger.info("Reached max_files limit (%d) -- stopping scan.", max_files)
                break

        logger.info("Scan complete: %d files found in %s", len(found), root)
        return found

    async def scan_async(
        self,
        folder_path: str | Path,
        recursive: bool = True,
        max_files: int = 50,
    ) -> List[SourceFile]:
        """
        Async wrapper -- offloads the blocking scan to the thread pool so
        the event loop stays free during large folder walks.
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            _io_pool, lambda: self.scan(folder_path, recursive, max_files)
        )

    def scan_summary(self, folder_path: str | Path, recursive: bool = True) -> dict:
        """Return stats about the folder without loading file contents."""
        root = Path(folder_path).resolve()
        total = 0
        skipped_size = 0
        skipped_ext = 0
        by_ext: dict[str, int] = {}

        pattern = "**/*" if recursive else "*"
        for p in root.glob(pattern):
            if not p.is_file():
                continue
            if any(part in self._excluded_dirs for part in p.parts):
                continue

            ext = p.suffix.lower()
            if ext not in self._included_exts:
                skipped_ext += 1
                continue

            size_kb = p.stat().st_size / 1024
            if size_kb > self._max_size_kb:
                skipped_size += 1
                continue

            total += 1
            by_ext[ext] = by_ext.get(ext, 0) + 1

        return {
            "total_reviewable_files": total,
            "skipped_size_limit": skipped_size,
            "skipped_unknown_extension": skipped_ext,
            "by_extension": by_ext,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _walk(self, root: Path, recursive: bool) -> Iterator[SourceFile]:
        pattern = "**/*" if recursive else "*"
        candidates = sorted(root.glob(pattern))  # Deterministic order

        for path in candidates:
            if not path.is_file():
                continue

            # Skip excluded directories anywhere in the path
            if any(part in self._excluded_dirs for part in path.relative_to(root).parts):
                continue

            ext = path.suffix.lower()
            if ext not in self._included_exts:
                continue

            stat = path.stat()
            size_kb = stat.st_size / 1024

            if size_kb > self._max_size_kb:
                logger.debug("Skipping large file: %s (%.1f KB)", path, size_kb)
                continue

            try:
                # read_text is blocking I/O -- acceptable here because _walk is
                # always called from scan(), which runs on the thread pool
                # when invoked via scan_async().
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                logger.warning("Cannot read %s: %s", path, e)
                continue

            if should_skip_file(content, self._max_size_kb):
                continue

            rel = str(path.relative_to(root)).replace("\\", "/")
            lang = _ext_to_language(ext)

            yield SourceFile(
                path=path,
                relative_path=rel,
                size_bytes=stat.st_size,
                content=content,
                language=lang,
            )


# ---------------------------------------------------------------------------
# Language detection helper
# ---------------------------------------------------------------------------

_EXT_MAP = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".jsx": "javascript",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".cpp": "cpp",
    ".c": "c",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
    ".kt": "kotlin",
}


def _ext_to_language(ext: str) -> Optional[str]:
    return _EXT_MAP.get(ext)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_service: ScannerService | None = None


def get_scanner_service() -> ScannerService:
    global _service
    if _service is None:
        _service = ScannerService()
    return _service

# --- END OF FILE: scanner_service.py ---
