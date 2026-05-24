"""
circuit_breaker.py — Per-provider circuit breaker for the LLM service.

States:
  CLOSED    → normal, requests flow through
  OPEN      → provider is cooling down, requests skip immediately
  HALF-OPEN → cooldown elapsed, one probe allowed to test recovery

Usage:
    breaker = get_circuit_breaker()
    if not await breaker.is_available("nvidia"):
        ...  # skip this provider
    try:
        result = await call_provider(...)
        await breaker.record_success("nvidia")
    except Exception:
        await breaker.record_failure("nvidia")
        raise
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict

logger = logging.getLogger(__name__)


class CircuitBreaker:
    """
    Async-safe per-provider circuit breaker.

    Args:
        failure_threshold:  Consecutive failures before a provider is marked OPEN.
        cooldown_seconds:   Seconds to wait in OPEN state before allowing a probe.
    """

    def __init__(self, failure_threshold: int = 3, cooldown_seconds: float = 60.0):
        self._threshold = failure_threshold
        self._cooldown = cooldown_seconds

        # Per-provider failure state (circuit open/closed)
        self._failures: Dict[str, int] = {}
        self._opened_at: Dict[str, float] = {}
        self._lock = asyncio.Lock()

        # Per-provider typed error counters (lifetime totals, never reset)
        self._rate_limit_hits: Dict[str, int] = {}   # HTTP 429
        self._auth_errors: Dict[str, int] = {}       # HTTP 401/403
        self._fallback_counts: Dict[str, int] = {}   # times used as non-primary fallback

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def is_available(self, provider: str) -> bool:
        """Return True if the provider should be tried (CLOSED or HALF-OPEN)."""
        async with self._lock:
            opened = self._opened_at.get(provider)
            if opened is None:
                return True  # CLOSED — normal operation

            elapsed = time.monotonic() - opened
            if elapsed >= self._cooldown:
                # Transition to HALF-OPEN: allow one probe without resetting state yet
                logger.info(
                    "Circuit HALF-OPEN for %s (was open for %.0fs, cooldown=%.0fs)",
                    provider, elapsed, self._cooldown,
                )
                return True

            # Still OPEN
            remaining = self._cooldown - elapsed
            logger.debug(
                "Circuit OPEN for %s — skipping (%.0fs remaining in cooldown)",
                provider, remaining,
            )
            return False

    async def record_success(self, provider: str) -> None:
        """Reset failure count and close the circuit on a successful call."""
        async with self._lock:
            had_failures = self._failures.get(provider, 0)
            self._failures.pop(provider, None)
            self._opened_at.pop(provider, None)
            if had_failures:
                logger.info("Circuit CLOSED for %s (recovered after %d failure(s))", provider, had_failures)

    async def record_failure(self, provider: str, *, is_auth_error: bool = False) -> None:
        """
        Increment the failure counter.

        Auth errors (403) count as double failures because they indicate a
        misconfigured key that will keep failing until manually fixed.
        """
        async with self._lock:
            increment = 2 if is_auth_error else 1
            count = self._failures.get(provider, 0) + increment
            self._failures[provider] = count

            if count >= self._threshold and provider not in self._opened_at:
                self._opened_at[provider] = time.monotonic()
                logger.warning(
                    "Circuit OPEN for %s after %d failure point(s) "
                    "(threshold=%d, cooldown=%.0fs)",
                    provider, count, self._threshold, self._cooldown,
                )

    # ------------------------------------------------------------------
    # Typed event counters (never reset — lifetime totals for display)
    # ------------------------------------------------------------------

    async def record_rate_limit(self, provider: str) -> None:
        """Call when a provider returns HTTP 429."""
        async with self._lock:
            self._rate_limit_hits[provider] = self._rate_limit_hits.get(provider, 0) + 1
        logger.warning("[429] Rate limited on %s (total hits: %d)",
                       provider, self._rate_limit_hits[provider])

    async def record_auth_error(self, provider: str) -> None:
        """Call when a provider returns HTTP 401 or 403."""
        async with self._lock:
            self._auth_errors[provider] = self._auth_errors.get(provider, 0) + 1
        logger.warning("[403] Auth error on %s — check API key/token (total: %d)",
                       provider, self._auth_errors[provider])

    async def record_fallback_used(self, provider: str) -> None:
        """Call when a provider is used as a fallback (not the primary choice)."""
        async with self._lock:
            self._fallback_counts[provider] = self._fallback_counts.get(provider, 0) + 1

    # ------------------------------------------------------------------
    # Observability
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return current state of all tracked providers (for /health endpoint)."""
        now = time.monotonic()
        all_known = set(
            list(self._failures.keys()) +
            list(self._opened_at.keys()) +
            list(self._rate_limit_hits.keys()) +
            list(self._auth_errors.keys()) +
            list(self._fallback_counts.keys())
        )
        result = {}
        for provider in all_known:
            opened = self._opened_at.get(provider)
            if opened is None:
                state = "closed"
                cooldown_remaining = 0.0
            elif (now - opened) >= self._cooldown:
                state = "half-open"
                cooldown_remaining = 0.0
            else:
                state = "open"
                cooldown_remaining = round(self._cooldown - (now - opened), 1)
            result[provider] = {
                "state":                    state,
                "failures":                 self._failures.get(provider, 0),
                "cooldown_remaining_seconds": cooldown_remaining,
                # Typed lifetime counters
                "rate_limit_hits":          self._rate_limit_hits.get(provider, 0),
                "auth_errors":              self._auth_errors.get(provider, 0),
                "fallback_uses":            self._fallback_counts.get(provider, 0),
            }
        return result


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_breaker: CircuitBreaker | None = None


def get_circuit_breaker() -> CircuitBreaker:
    """Return the shared CircuitBreaker instance (created on first call)."""
    global _breaker
    if _breaker is None:
        _breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=60.0)
    return _breaker
