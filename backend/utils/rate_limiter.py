"""
rate_limiter.py — Token-bucket rate limiter for OpenAI API calls.

Tracks both requests/min and tokens/min to stay within OpenAI's limits.
Thread-safe using asyncio locks — safe for FastAPI's async handlers.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from typing import Deque, Tuple

from config import settings


class RateLimiter:
    """
    Sliding-window rate limiter that tracks:
    - Requests per minute (RPM)
    - Tokens per minute (TPM)

    Call `acquire(tokens)` before each OpenAI request.
    It will sleep until capacity is available.
    """

    def __init__(
        self,
        requests_per_minute: int,
        tokens_per_minute: int,
        window_seconds: int = 60,
    ):
        self._rpm = requests_per_minute
        self._tpm = tokens_per_minute
        self._window = window_seconds

        # Each entry: (timestamp, tokens_used)
        self._requests: Deque[Tuple[float, int]] = deque()
        self._lock = asyncio.Lock()

    def _evict_old(self, now: float) -> None:
        """Remove entries older than the sliding window."""
        cutoff = now - self._window
        while self._requests and self._requests[0][0] < cutoff:
            self._requests.popleft()

    def _current_usage(self, now: float) -> Tuple[int, int]:
        """Return (request_count, token_count) in the current window."""
        self._evict_old(now)
        req_count = len(self._requests)
        token_count = sum(t for _, t in self._requests)
        return req_count, token_count

    async def acquire(self, estimated_tokens: int = 1000) -> None:
        """
        Block until the rate limiter grants capacity.
        `estimated_tokens` is an upper-bound estimate for the request.
        """
        while True:
            wait = 0.0
            async with self._lock:
                now = time.monotonic()
                req_count, token_count = self._current_usage(now)

                # Prevent infinite deadlock if a single request exceeds TPM
                capped_tokens = min(estimated_tokens, self._tpm)
                req_ok = req_count < self._rpm
                tok_ok = token_count + capped_tokens <= self._tpm

                if req_ok and tok_ok:
                    self._requests.append((now, capped_tokens))
                    return

                # Calculate how long to wait for the oldest entry to expire
                if self._requests:
                    oldest = self._requests[0][0]
                    wait = (oldest + self._window) - now + 0.05
                    wait = max(0.1, min(wait, self._window))
                else:
                    wait = 1.0

            # Sleep OUTSIDE the lock so other requests can be processed concurrently
            await asyncio.sleep(wait)

    def status(self) -> dict:
        """Return current usage stats (for /health endpoint)."""
        now = time.monotonic()
        req_count, token_count = self._current_usage(now)
        return {
            "requests_used": req_count,
            "requests_limit": self._rpm,
            "tokens_used": token_count,
            "tokens_limit": self._tpm,
            "window_seconds": self._window,
        }


# ---------------------------------------------------------------------------
# Per-provider rate limiter singletons
# ---------------------------------------------------------------------------

# Dict keyed by provider name → RateLimiter
_limiters: dict[str, RateLimiter] = {}
_nvidia_multiplier: int = 0   # track what multiplier the nvidia limiter was built with


def _compute_multiplier() -> int:
    """Return the number of active NVIDIA keys (at least 1)."""
    from config import reload_nvidia_keys_if_changed
    reload_nvidia_keys_if_changed()
    return max(1, len(settings.nvidia_accounts))


def reset_rate_limiter() -> None:
    """Discard all per-provider limiters so they rebuild on next acquire().
    Call this whenever the key pool might have changed (e.g. at scan start)."""
    global _limiters, _nvidia_multiplier
    _limiters = {}
    _nvidia_multiplier = 0


def get_rate_limiter(provider: str = "nvidia") -> RateLimiter:
    """
    Return the rate limiter for *provider*.

    NVIDIA's limiter scales with the number of active API keys (round-robin pool).
    All other providers use the base RPM/TPM from config — they each get their
    own independent window so exhausting NVIDIA doesn't throttle DeepSeek etc.
    """
    global _limiters, _nvidia_multiplier

    if provider == "nvidia":
        multiplier = _compute_multiplier()
        # Rebuild the NVIDIA limiter if the key pool has grown/shrunk
        if "nvidia" not in _limiters or multiplier != _nvidia_multiplier:
            _nvidia_multiplier = multiplier
            _limiters["nvidia"] = RateLimiter(
                requests_per_minute=settings.rate_limit.requests_per_minute * multiplier,
                tokens_per_minute=settings.rate_limit.tokens_per_minute * multiplier,
            )
            import logging
            logging.getLogger(__name__).info(
                "NVIDIA rate limiter (re)built: %d key(s) × %d RPM / %d TPM = %d RPM / %d TPM",
                multiplier,
                settings.rate_limit.requests_per_minute,
                settings.rate_limit.tokens_per_minute,
                settings.rate_limit.requests_per_minute * multiplier,
                settings.rate_limit.tokens_per_minute * multiplier,
            )
    else:
        if provider not in _limiters:
            _limiters[provider] = RateLimiter(
                requests_per_minute=settings.rate_limit.requests_per_minute,
                tokens_per_minute=settings.rate_limit.tokens_per_minute,
            )
            import logging
            logging.getLogger(__name__).info(
                "Rate limiter built for provider '%s': %d RPM / %d TPM",
                provider,
                settings.rate_limit.requests_per_minute,
                settings.rate_limit.tokens_per_minute,
            )

    return _limiters[provider]


def all_limiter_status() -> dict:
    """Return status dict for every active per-provider limiter (for /health)."""
    return {name: lim.status() for name, lim in _limiters.items()}

# --- END OF FILE: rate_limiter.py ---
