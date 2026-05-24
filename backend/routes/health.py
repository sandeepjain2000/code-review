"""
routes/health.py — Health check and diagnostics endpoints.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from config import settings
from models.schemas import HealthResponse
from services.cache_service import get_cache
from utils.rate_limiter import get_rate_limiter, all_limiter_status
from utils.circuit_breaker import get_circuit_breaker

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse, summary="Health check")
async def health():
    """Basic health check — confirms the server is running and config is loaded."""
    return HealthResponse(
        status="ok",
        version="1.0.0",
        model=settings.openai.model,
        cache_enabled=settings.cache.enabled,
    )


@router.get("/health/detailed", summary="Detailed diagnostics")
async def health_detailed():
    """Returns rate limiter status, cache stats, and configuration summary."""
    cache = get_cache()

    cache_stats = {}
    try:
        cache_stats = await cache.stats()
    except Exception as e:
        cache_stats = {"error": str(e)}

    return {
        "status": "ok",
        "version": "1.0.0",
        "model": settings.openai.model,
        "rate_limiters": all_limiter_status(),
        "circuit_breakers": get_circuit_breaker().status(),
        "cache": {
            "enabled": settings.cache.enabled,
            "ttl_seconds": settings.cache.ttl_seconds,
            **cache_stats,
        },
        "scanner": {
            "excluded_dirs": settings.scanner.excluded_dirs,
            "included_extensions": settings.scanner.included_extensions,
            "max_file_size_kb": settings.scanner.max_file_size_kb,
        },
        "github": {
            "token_configured": bool(settings.github.token),
            "webhook_secret_configured": bool(settings.github.webhook_secret),
        },
    }


@router.get("/health/providers", summary="Live provider health (circuit breakers + rate limits)")
async def health_providers():
    """
    Returns real-time status for every AI provider:
      - Circuit breaker state (closed / open / half-open)
      - Consecutive failure count
      - Cooldown remaining (seconds) when open
      - Current rate-limiter usage (requests + tokens in sliding window)

    Designed to be polled by the frontend every few seconds during a scan.
    """
    breaker_status = get_circuit_breaker().status()    # keyed by provider name
    limiter_status = all_limiter_status()              # keyed by provider name

    # Merge: every provider that appears in either dict gets a combined entry
    all_providers = sorted(set(list(breaker_status.keys()) + list(limiter_status.keys())))

    providers = {}
    for p in all_providers:
        cb  = breaker_status.get(p, {"state": "closed", "failures": 0, "cooldown_remaining_seconds": 0})
        rl  = limiter_status.get(p, {})
        providers[p] = {
            "circuit": cb,
            "rate_limiter": rl,
        }

    # Always include providers that have keys configured, even if never used yet
    known = ["nvidia", "deepseek", "openai", "claude", "gemini"]
    for p in known:
        if p not in providers:
            providers[p] = {
                "circuit": {"state": "closed", "failures": 0, "cooldown_remaining_seconds": 0},
                "rate_limiter": {},
            }

    return {"providers": providers}
