"""
main.py -- FastAPI application entry point.

Run with:
    uvicorn main:app --reload --port 8000

Or via the config:
    python main.py
"""

from __future__ import annotations

import json as _json
import logging
import sys
import uuid
from contextvars import ContextVar
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from config import settings
from routes.github import router as github_router
from routes.health import router as health_router
from routes.review import router as review_router

# ---------------------------------------------------------------------------
# Structured logging
# ---------------------------------------------------------------------------

_request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class _JSONFormatter(logging.Formatter):
    """Emit one JSON object per log line -- easy to ingest with ELK / Datadog."""
    def format(self, record: logging.LogRecord) -> str:
        obj: dict = {
            "ts":         self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level":      record.levelname,
            "logger":     record.name,
            "msg":        record.getMessage(),
            "request_id": _request_id_var.get("-"),
        }
        if record.exc_info:
            obj["exc"] = self.formatException(record.exc_info)
        return _json.dumps(obj)


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    if settings.server.debug:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
    else:
        handler.setFormatter(_JSONFormatter())
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if settings.server.debug else logging.INFO)
    root.handlers = [handler]

    # Silence verbose third-party logging
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


_configure_logging()
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# App lifecycle
# ---------------------------------------------------------------------------

def _active_provider_label() -> str:
    """Return a human-readable label for whichever provider will be used."""
    p = settings.provider.lower()
    if p == "openai":
        return f"OpenAI ({settings.openai.model})"
    if p == "claude":
        return f"Claude ({settings.claude.model})"
    if p == "gemini":
        return f"Gemini ({settings.gemini.model})"
    if p == "nvidia":
        return f"NVIDIA NIM ({settings.nvidia.model})"
    if p == "deepseek":
        return f"DeepSeek ({settings.deepseek.model})"
    # auto -- list all configured providers (in priority order)
    parts = []
    if settings.nvidia_accounts:
        n = len(settings.nvidia_accounts)
        suffix = f" ({n} keys)" if n > 1 else ""
        parts.append(f"NVIDIA/{settings.nvidia.model}{suffix}")
    else:
        key = settings.nvidia.api_key
        if key and key.startswith("nvapi-") and "placeholder" not in key and "your-" not in key:
            parts.append(f"NVIDIA/{settings.nvidia.model}")
    key = settings.deepseek.api_key
    if key and key.startswith("sk-") and "placeholder" not in key and "your-" not in key:
        parts.append(f"DeepSeek/{settings.deepseek.model}")
    key = settings.openai.api_key
    if key and key.startswith("sk-") and "placeholder" not in key and "your-" not in key:
        parts.append(f"OpenAI/{settings.openai.model}")
    key = settings.claude.api_key
    if key and key.startswith("sk-ant-") and "placeholder" not in key and "your-" not in key:
        parts.append(f"Claude/{settings.claude.model}")
    key = settings.gemini.api_key
    if key and len(key) > 10 and "placeholder" not in key and "your-" not in key:
        parts.append(f"Gemini/{settings.gemini.model}")
    return "auto -> " + (", ".join(parts) if parts else "NO VALID KEYS FOUND")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("=" * 60)
    logger.info("  CodeSentinel v1.0.0 - AI Code Review API")
    logger.info("  Provider : %s", _active_provider_label())
    logger.info("  Cache    : %s (TTL %ds)", "enabled" if settings.cache.enabled else "disabled", settings.cache.ttl_seconds)
    host_display = "localhost" if settings.server.host == "0.0.0.0" else settings.server.host
    logger.info("  Host     : http://%s:%d", host_display, settings.server.port)
    logger.info("  Docs     : http://%s:%d/docs", host_display, settings.server.port)
    logger.info("  Web UI   : http://%s:%d/ui  (open in browser; avoid file://)", host_display, settings.server.port)
    logger.info("=" * 60)
    yield
    logger.info("CodeSentinel shutting down.")


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = FastAPI(
    title="CodeSentinel - AI Code Review API",
    description=(
        "A production-ready code review API powered by GPT-4o / Claude / Gemini / DeepSeek Coder / NVIDIA NIM. "
        "Review GitHub PRs, uploaded files, pasted snippets, or entire folder trees. "
        "Returns structured JSON with line-level issues, severity ratings, and suggested fixes."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.server.allowed_origins + ["null"],
    # Allows any localhost / 127.0.0.1 port for local development, plus file:/// ('null').
    # In production, remove this regex and list only your real domains in allowed_origins.
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?|null",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:8]
    _request_id_var.set(request_id)
    logger.info("-> %s %s", request.method, request.url.path)
    response = await call_next(request)
    response.headers["X-Request-ID"] = request_id
    logger.info("<- %s %s %d", request.method, request.url.path, response.status_code)
    return response


# ---------------------------------------------------------------------------
# Global exception handler
# ---------------------------------------------------------------------------

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    # Log the full exception internally -- never expose it to the client
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"detail": "Internal server error"},
    )


# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------

app.include_router(health_router)
app.include_router(review_router)
app.include_router(github_router)


# ---------------------------------------------------------------------------
# Root + static UI (avoid opening frontend/index.html via file:// — CDNs / Babel often fail)
# ---------------------------------------------------------------------------

_FRONTEND_INDEX = Path(__file__).resolve().parent.parent / "frontend" / "index.html"


@app.get("/", include_in_schema=False)
async def root():
    return {
        "name": "CodeSentinel",
        "version": "1.0.0",
        "ui": "/ui",
        "docs": "/docs",
        "health": "/health",
    }


@app.get("/ui", include_in_schema=False)
async def web_ui():
    """Serve the React UI from the same host as the API (recommended)."""
    if not _FRONTEND_INDEX.is_file():
        return JSONResponse(
            status_code=500,
            content={"detail": "frontend/index.html not found", "expected": str(_FRONTEND_INDEX)},
        )
    return FileResponse(_FRONTEND_INDEX, media_type="text/html; charset=utf-8")


# ---------------------------------------------------------------------------
# Dev runner
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=settings.server.host,
        port=settings.server.port,
        reload=settings.server.debug,
        log_level="debug" if settings.server.debug else "info",
    )

# --- END OF FILE: main.py ---
