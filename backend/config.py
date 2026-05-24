"""
config.py -- Loads all configuration from api_key.json (or a path you specify).
Every service imports `settings` from here -- one source of truth.
"""

import json
import os
from pathlib import Path
from typing import List, Optional
from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Sub-config models
# ---------------------------------------------------------------------------

class OpenAIConfig(BaseModel):
    api_key: str = ""
    model: str = "gpt-4o"
    max_tokens: int = 8192
    temperature: float = 0.1


class ClaudeConfig(BaseModel):
    api_key: str = ""
    model: str = "claude-opus-4-6"
    max_tokens: int = 8192
    temperature: float = 0.1


class GeminiConfig(BaseModel):
    api_key: str = ""
    model: str = "gemini-1.5-pro"
    max_tokens: int = 8192
    temperature: float = 0.1


class NvidiaConfig(BaseModel):
    api_key: str = ""
    model: str = "meta/llama-3.3-70b-instruct"
    max_tokens: int = 8192
    temperature: float = 0.1
    base_url: str = "https://integrate.api.nvidia.com/v1"
    key_id: Optional[str] = None


class DeepSeekConfig(BaseModel):
    api_key: str = ""
    model: str = "deepseek-chat"
    max_tokens: int = 8192
    temperature: float = 0.1
    base_url: str = "https://api.deepseek.com"


class GitHubConfig(BaseModel):
    token: Optional[str] = None
    webhook_secret: Optional[str] = None


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    # Restrict in production: e.g. ["https://yourapp.com"]
    allowed_origins: List[str] = ["http://localhost:3000", "http://localhost:8080", "http://127.0.0.1:5500"]


class CacheConfig(BaseModel):
    enabled: bool = True
    ttl_seconds: int = 3600


class RateLimitConfig(BaseModel):
    requests_per_minute: int = 20
    tokens_per_minute: int = 80000


class ScannerConfig(BaseModel):
    max_file_size_kb: int = 500
    # Abort a folder scan if this many files fail consecutively (circuit breaker)
    max_consecutive_failures: int = 5
    # Max parallel file reviews within a batch.
    # 0 = auto (derived from the number of active NVIDIA keys at runtime, no hard cap).
    # Set explicitly (e.g. 25) in api_key.json to override the auto value.
    max_concurrent_files: int = 0
    excluded_dirs: List[str] = [
        "node_modules", ".git", "__pycache__", ".venv",
        "dist", "build", ".next", ".idea", ".vscode",
        ".netlify", "static", "chunks", "coverage",
        ".turbo", ".vercel", "out", ".output", "public",
        "backups", ".agents", "scratch",
    ]
    included_extensions: List[str] = [
        ".py", ".js", ".ts", ".tsx", ".jsx",
        ".java", ".go", ".rs", ".cpp", ".c",
        ".cs", ".rb", ".php", ".swift", ".kt",
    ]
    schema_file: Optional[str] = None


class Settings(BaseModel):
    provider: str = "auto"   # "auto" | "openai" | "claude" | "gemini" | "nvidia" | "deepseek"
    # All runtime data (reports, cache DB, jobs DB) lives here -- configurable
    # via DATA_DIR env var or the data_dir key in api_key.json
    data_dir: str = "data"
    batch_size: int = 25     # Files per batch; 0 = no batching
    # Relative to the directory that contains the main config JSON (or cwd if no file).
    # Each *.json file in that folder is merged onto `nvidia` defaults (see _finalize_nvidia_accounts).
    nvidia_keys_directory: Optional[str] = None
    openai: OpenAIConfig = OpenAIConfig()
    claude: ClaudeConfig = ClaudeConfig()
    gemini: GeminiConfig = GeminiConfig()
    nvidia: NvidiaConfig = NvidiaConfig()
    # Filled at load time from nvidia_keys_directory and/or nvidia.api_key — do not set in JSON.
    nvidia_accounts: List[NvidiaConfig] = Field(default_factory=list)
    deepseek: DeepSeekConfig = DeepSeekConfig()
    github: GitHubConfig = GitHubConfig()
    server: ServerConfig = ServerConfig()
    cache: CacheConfig = CacheConfig()
    rate_limit: RateLimitConfig = RateLimitConfig()
    scanner: ScannerConfig = ScannerConfig()


# ---------------------------------------------------------------------------
# Loader -- searches for api_key.json relative to the project root
# ---------------------------------------------------------------------------

def _find_config_file() -> Path:
    """
    Search order:
    1. ENV var: CODE_REVIEW_CONFIG (absolute path)
    2. Current working directory
    3. One level up (project root when running from backend/)
    """
    env_path = os.environ.get("CODE_REVIEW_CONFIG")
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
        raise FileNotFoundError(f"Config path from CODE_REVIEW_CONFIG not found: {env_path}")

    candidates = [
        Path.cwd() / "api_key.json",
        Path(__file__).parent.parent / "api_key.json",
        Path(__file__).parent / "api_key.json",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate

    raise FileNotFoundError(
        "api_key.json not found. Copy api_key.json.example -> api_key.json "
        "and fill in your keys, or set the CODE_REVIEW_CONFIG environment variable."
    )


def _apply_env_overrides(s: Settings) -> Settings:
    """
    Let environment variables override JSON-file values for sensitive fields.
    This allows secrets to be injected at deploy time without touching api_key.json.
    """
    env = os.environ

    def _pick(env_key: str, current: str) -> str:
        v = env.get(env_key, "").strip()
        return v if v else current

    s.openai.api_key        = _pick("OPENAI_API_KEY",        s.openai.api_key)
    s.claude.api_key        = _pick("ANTHROPIC_API_KEY",     s.claude.api_key)
    s.gemini.api_key        = _pick("GOOGLE_API_KEY",        s.gemini.api_key)
    s.nvidia.api_key        = _pick("NVIDIA_API_KEY",        s.nvidia.api_key)
    s.deepseek.api_key      = _pick("DEEPSEEK_API_KEY",      s.deepseek.api_key)
    s.github.token          = _pick("GITHUB_TOKEN",          s.github.token or "") or None
    s.github.webhook_secret = _pick("GITHUB_WEBHOOK_SECRET", s.github.webhook_secret or "") or None
    s.data_dir              = _pick("DATA_DIR",              s.data_dir)

    return s


def _nvidia_key_usable(key: str) -> bool:
    if not key or key in ("your-nvidia-key-here", ""):
        return False
    if not key.startswith("nvapi-"):
        return False
    return len(key) > 20


def load_expired_keys() -> dict:
    try:
        config_path = _find_config_file()
        path = config_path.parent / "expired_keys.json"
    except Exception:
        path = Path.cwd() / "expired_keys.json"

    if not path.is_file():
        return {"key_ids": [], "api_keys": []}
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {"key_ids": [], "api_keys": []}
            return {
                "key_ids": data.get("key_ids", []),
                "api_keys": data.get("api_keys", [])
            }
    except Exception:
        return {"key_ids": [], "api_keys": []}


def mark_key_expired(key_id: str, api_key: str) -> None:
    config_path = None
    try:
        config_path = _find_config_file()
        path = config_path.parent / "expired_keys.json"
    except Exception:
        path = Path.cwd() / "expired_keys.json"

    expired = load_expired_keys()
    changed = False
    if key_id and key_id not in expired["key_ids"]:
        expired["key_ids"].append(key_id)
        changed = True
    if api_key and api_key not in expired["api_keys"]:
        expired["api_keys"].append(api_key)
        changed = True

    if changed:
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(expired, f, indent=2)
            # Trigger a reload of settings to filter out the newly expired key
            if config_path:
                _finalize_nvidia_accounts(config_path, settings)
        except Exception:
            pass


def _finalize_nvidia_accounts(config_path: Optional[Path], s: Settings) -> Settings:
    """
    Build `nvidia_accounts` from optional per-key JSON files, else from `nvidia.api_key`.

    Each file in `nvidia_keys_directory` is merged onto `s.nvidia` (model, base_url, etc.);
    the file should at least set `api_key`. Invalid / missing keys are skipped.
    """
    base_dir = config_path.parent if config_path else Path.cwd()
    accounts: List[NvidiaConfig] = []

    # Filter out expired keys
    expired = load_expired_keys()
    expired_ids = set(expired.get("key_ids", []))
    expired_keys = set(expired.get("api_keys", []))

    if s.nvidia_keys_directory:
        keys_dir = (base_dir / s.nvidia_keys_directory).resolve()
        if keys_dir.is_dir():
            defaults = s.nvidia.model_dump()
            for p in sorted(keys_dir.glob("*.json")):
                # Skip templates committed to the repo (e.g. *.example.json)
                if p.name.startswith(("_", ".")) or ".example" in p.name.lower():
                    continue
                if p.name == "expired_keys.json" or p.name in expired_ids:
                    continue
                try:
                    with open(p, encoding="utf-8-sig") as f:
                        data = json.load(f)
                    if not isinstance(data, dict):
                        continue
                    merged = {**defaults, **data}
                    acc = NvidiaConfig(**merged)
                    acc.key_id = p.name  # e.g. "key1.json"
                    if acc.api_key in expired_keys:
                        continue
                    if _nvidia_key_usable(acc.api_key):
                        accounts.append(acc)
                except (OSError, json.JSONDecodeError, TypeError, ValueError):
                    continue

    if not accounts and _nvidia_key_usable(s.nvidia.api_key):
        acc = s.nvidia.model_copy(deep=True)
        acc.key_id = "default"
        if acc.key_id not in expired_ids and acc.api_key not in expired_keys:
            accounts = [acc]

    s.nvidia_accounts = accounts
    if accounts:
        s.nvidia = accounts[0].model_copy(deep=True)
    return s


def load_settings() -> Settings:
    config_path: Optional[Path] = None
    try:
        config_path = _find_config_file()
        with open(config_path, encoding="utf-8-sig") as f:
            raw = json.load(f)
        s = Settings(**raw)
    except FileNotFoundError:
        s = Settings()
    s = _apply_env_overrides(s)
    return _finalize_nvidia_accounts(config_path, s)


# Singleton -- imported everywhere
settings: Settings = load_settings()

_last_keys_mtime: float = 0.0

def reload_nvidia_keys_if_changed() -> None:
    """Check if the nvidia_keys directory has changed (files added/removed), and reload if so."""
    global _last_keys_mtime
    if not settings.nvidia_keys_directory:
        return
    try:
        config_path = _find_config_file()
        base_dir = config_path.parent if config_path else Path.cwd()
        keys_dir = (base_dir / settings.nvidia_keys_directory).resolve()
        if not keys_dir.is_dir():
            return
            
        current_mtime = keys_dir.stat().st_mtime
        # Also check mtime of all json files inside, in case a file was edited in-place
        for p in keys_dir.glob("*.json"):
            current_mtime = max(current_mtime, p.stat().st_mtime)
            
        if current_mtime > _last_keys_mtime:
            _last_keys_mtime = current_mtime
            _finalize_nvidia_accounts(config_path, settings)
    except Exception:
        pass

# Initialize mtime on startup
reload_nvidia_keys_if_changed()

# --- END OF FILE: config.py ---
