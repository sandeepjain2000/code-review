"""
chunking.py — Splits large source files into overlapping chunks for OpenAI review.

Strategy:
- Respect logical boundaries (functions, classes) where possible
- Add overlap between chunks so issues spanning boundaries aren't missed
- Track original line numbers so issues map back correctly
- Use tiktoken to stay within model context limits
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

try:
    import tiktoken
    _ENCODER = tiktoken.encoding_for_model("gpt-4o")
except Exception:
    _ENCODER = None


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class CodeChunk:
    """A slice of source code ready to send to the model."""
    content: str            # The actual code text
    start_line: int         # 1-based line number in the original file
    end_line: int           # 1-based line number in the original file
    chunk_index: int        # 0-based index in the chunk list
    total_chunks: int       # Total number of chunks for this file
    token_count: int = 0    # Approximate token count

    @property
    def info_string(self) -> str:
        """Human-readable annotation injected into the prompt."""
        is_first = self.chunk_index == 0
        is_last  = self.chunk_index == self.total_chunks - 1
        if is_first and is_last:
            position = ""   # Single chunk, no partial-file warning needed
        elif is_first:
            position = "(first chunk — closing braces/returns from later chunks are not shown)"
        elif is_last:
            position = "(last chunk — top-of-file imports and earlier definitions are not shown)"
        else:
            position = "(middle chunk — both imports and closing braces are in other chunks)"
        return (
            f"⚠️  PARTIAL FILE — Chunk {self.chunk_index + 1} of {self.total_chunks} "
            f"{position}. "
            f"Lines shown: {self.start_line}–{self.end_line} of the original file. "
            f"Line numbers below are ORIGINAL line numbers. "
            f"DO NOT flag undefined symbols, missing imports, or truncated functions "
            f"that may simply be defined outside this chunk. "
            f"Cap severity at MEDIUM for any issue that requires full-file context to confirm."
        )


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

def count_tokens(text: str) -> int:
    if _ENCODER is not None:
        return len(_ENCODER.encode(text))
    # Rough fallback: ~4 chars per token
    return len(text) // 4


# ---------------------------------------------------------------------------
# Main chunker
# ---------------------------------------------------------------------------

# Patterns that mark the start of a logical unit (function/class definitions)
_BOUNDARY_PATTERNS = [
    re.compile(r"^(async\s+)?def\s+\w+", re.MULTILINE),      # Python functions
    re.compile(r"^class\s+\w+", re.MULTILINE),                 # Python/JS classes
    re.compile(r"^(export\s+)?(async\s+)?function\s+\w+", re.MULTILINE),  # JS functions
    re.compile(r"^(public|private|protected|static|\s)+\w+\s+\w+\s*\(", re.MULTILINE),  # Java/C# methods
    re.compile(r"^func\s+\w+", re.MULTILINE),                  # Go functions
    re.compile(r"^fn\s+\w+", re.MULTILINE),                    # Rust functions
]


def _find_logical_boundaries(lines: List[str]) -> List[int]:
    """Return a sorted list of line indices that start logical units."""
    text = "\n".join(lines)
    boundaries = set([0])  # Always include the start

    for pattern in _BOUNDARY_PATTERNS:
        for match in pattern.finditer(text):
            # Convert char offset to line index
            line_idx = text[:match.start()].count("\n")
            boundaries.add(line_idx)

    return sorted(boundaries)


def chunk_code(
    code: str,
    max_tokens_per_chunk: int = 3000,
    overlap_lines: int = 20,
) -> List[CodeChunk]:
    """
    Split `code` into chunks that fit within `max_tokens_per_chunk`.

    Returns a single chunk if the code fits without splitting.
    Adds `overlap_lines` of context at the start of each chunk (except the first)
    so the model doesn't lose context at boundaries.
    """
    lines = code.splitlines()
    total_lines = len(lines)

    # Fast path: fits in one chunk
    if count_tokens(code) <= max_tokens_per_chunk:
        return [CodeChunk(
            content=code,
            start_line=1,
            end_line=total_lines,
            chunk_index=0,
            total_chunks=1,
            token_count=count_tokens(code),
        )]

    boundaries = _find_logical_boundaries(lines)
    chunks: List[CodeChunk] = []
    chunk_start = 0  # 0-based index into lines

    while chunk_start < total_lines:
        # Greedily add lines until we'd exceed the token budget
        chunk_end = chunk_start
        accumulated = ""

        for i in range(chunk_start, total_lines):
            candidate = accumulated + lines[i] + "\n"
            if count_tokens(candidate) > max_tokens_per_chunk and i > chunk_start:
                # Try to break at the nearest logical boundary before i
                best_break = chunk_start
                for b in boundaries:
                    if chunk_start < b <= i:
                        best_break = b
                chunk_end = best_break if best_break > chunk_start else i
                break
            accumulated = candidate
            chunk_end = i + 1  # exclusive

        chunk_lines = lines[chunk_start:chunk_end]
        chunk_content = "\n".join(chunk_lines)

        chunks.append(CodeChunk(
            content=chunk_content,
            start_line=chunk_start + 1,   # 1-based
            end_line=chunk_end,            # 1-based (inclusive)
            chunk_index=len(chunks),
            total_chunks=0,               # Fixed up below
            token_count=count_tokens(chunk_content),
        ))

        # Next chunk starts with overlap for context continuity
        chunk_start = max(chunk_start + 1, chunk_end - overlap_lines)

    # Fix up total_chunks now that we know how many there are
    total = len(chunks)
    for chunk in chunks:
        chunk.total_chunks = total

    return chunks


def add_line_numbers_with_offset(code: str, start_line: int) -> str:
    """
    Add line numbers starting from `start_line` so cited lines
    match the original file, not the chunk.
    """
    lines = code.splitlines()
    width = len(str(start_line + len(lines)))
    return "\n".join(
        f"{str(start_line + i).rjust(width)}: {line}"
        for i, line in enumerate(lines)
    )


# ---------------------------------------------------------------------------
# File size guard
# ---------------------------------------------------------------------------

def should_skip_file(content: str, max_kb: int = 500) -> bool:
    """Return True if the file is too large to review meaningfully."""
    return len(content.encode("utf-8")) > max_kb * 1024

# --- END OF FILE: chunking.py ---
