"""
llm_service.py -- Multi-provider AI service (OpenAI, Claude, Gemini, NVIDIA NIM).

Provider selection (set "provider" in api_key.json):
  "auto"   -- tries providers in order based on which keys look valid
  "openai" -- always use OpenAI
  "claude" -- always use Anthropic Claude
  "gemini" -- always use Google Gemini
  "nvidia" -- always use NVIDIA NIM

NVIDIA keys can live in separate JSON files: set "nvidia_keys_directory" in the main
config to a folder (relative to that JSON file) containing one *.json per key; each
file is merged onto the main "nvidia" block (model, base_url, etc.). Multiple keys
are used in round-robin order per API call.

Renamed from openai_service.py -- this file handles ALL LLM providers, not just OpenAI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from collections import Counter
from typing import List, Optional

from config import settings
from models.schemas import CodeIssue, FileReview, IssueType
from services.cache_service import get_cache
from utils.chunking import CodeChunk, add_line_numbers_with_offset, chunk_code, count_tokens
from utils.prompts import (
    DIFF_SYSTEM_PROMPT,
    SYSTEM_PROMPT,
    build_diff_prompt,
    build_multi_file_context_prompt,
    build_review_prompt,
)
from utils.circuit_breaker import get_circuit_breaker
from utils.rate_limiter import get_rate_limiter

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Structured output schema -- used by providers that support it natively
#
# IMPORTANT: OpenAI strict mode requires:
#   - Every property listed in "properties" MUST also appear in "required"
#   - Optional fields use {"type": ["actual_type", "null"]}
#   - "additionalProperties": false at every object level
# Violating any of these causes a 400 BadRequestError on every call.
# ---------------------------------------------------------------------------

_REVIEW_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "score":   {"type": ["integer", "null"]},      # nullable -- required by strict mode
        "test_cases": {
            "type": "array",
            "items": {"type": "string"}
        },
        "issues": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "line":          {"type": ["integer", "null"]},
                    "line_end":      {"type": ["integer", "null"]},
                    "severity":      {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                    "type":          {"type": "string"},
                    "message":       {"type": "string"},
                    "suggestion":    {"type": ["string", "null"]},
                    "code_snippet":  {"type": ["string", "null"]},
                    "fixed_snippet": {"type": ["string", "null"]},
                },
                # ALL properties must be in required for OpenAI strict mode
                "required": [
                    "line", "line_end", "severity", "type",
                    "message", "suggestion", "code_snippet", "fixed_snippet",
                ],
                "additionalProperties": False,
            },
        },
    },
    # ALL top-level properties must be in required for OpenAI strict mode
    "required": ["summary", "score", "test_cases", "issues"],
    "additionalProperties": False,
}


# ---------------------------------------------------------------------------
# Provider detection helpers
# ---------------------------------------------------------------------------

def _is_valid_key(key: str, prefix: str = "") -> bool:
    if not key or key in ("your-anthropic-key-here", "your-gemini-key-here", ""):
        return False
    if prefix and not key.startswith(prefix):
        return False
    return len(key) > 20


def _get_provider_order() -> list[str]:
    """
    Return providers to try, in priority order:
      1. NVIDIA  (primary)
      2. DeepSeek (first fallback)
      3. OpenAI or Claude — random choice each call (second fallback)
      4. Gemini  (third fallback, if configured)
      5. NVIDIA  again (final retry for transient errors)
    """
    pref = settings.provider.lower().strip()
    if pref != "auto":
        return [pref]

    order: list[str] = []

    # 1. NVIDIA first
    if settings.nvidia_accounts or _is_valid_key(settings.nvidia.api_key, "nvapi-"):
        order.append("nvidia")

    # 2. DeepSeek second
    if _is_valid_key(settings.deepseek.api_key, "sk-"):
        order.append("deepseek")

    # 3. OpenAI / Claude — random choice so both share load over time
    tier3: list[str] = []
    if _is_valid_key(settings.openai.api_key, "sk-"):
        tier3.append("openai")
    if _is_valid_key(settings.claude.api_key, "sk-ant-"):
        tier3.append("claude")
    if tier3:
        order.append(random.choice(tier3))
        # Also add the other one so it can be reached if the random pick fails
        if len(tier3) == 2:
            other = [p for p in tier3 if p != order[-1]][0]
            order.append(other)

    # 4. Gemini as last resort
    if _is_valid_key(settings.gemini.api_key):
        order.append("gemini")

    # 5. Retry NVIDIA (catches transient errors on the primary)
    if "nvidia" in order:
        order.append("nvidia")

    if not order:
        logger.warning("No valid AI provider keys found -- defaulting to openai")
        order = ["openai"]

    return order


def _active_provider() -> str:
    return _get_provider_order()[0]


def _active_model() -> str:
    p = _active_provider()
    if p == "claude":
        return settings.claude.model
    if p == "gemini":
        return settings.gemini.model
    if p == "nvidia":
        return f"nvidia/{settings.nvidia.model}"
    if p == "deepseek":
        return f"deepseek/{settings.deepseek.model}"
    return settings.openai.model


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------

class LLMService:
    """Multi-provider AI service -- abstracts OpenAI, Claude, Gemini, and NVIDIA NIM."""

    def __init__(self):
        self._cache = get_cache()
        self._model = _active_model()
        self._max_tokens = settings.openai.max_tokens
        self._temperature = settings.openai.temperature
        self._nvidia_rr = 0
        self._nvidia_lock = asyncio.Lock()

        self._openai_client = None
        self._claude_client = None

    async def _next_nvidia_account(self):
        """Round-robin across `settings.nvidia_accounts` when multiple key files exist."""
        from config import reload_nvidia_keys_if_changed
        reload_nvidia_keys_if_changed()
        
        accs = settings.nvidia_accounts
        if not accs:
            return settings.nvidia
        async with self._nvidia_lock:
            i = self._nvidia_rr % len(accs)
            self._nvidia_rr += 1
            return accs[i]

    def _get_openai(self):
        if self._openai_client is None:
            from openai import AsyncOpenAI
            self._openai_client = AsyncOpenAI(api_key=settings.openai.api_key, max_retries=0)
        return self._openai_client

    def _get_claude(self):
        if self._claude_client is None:
            from anthropic import AsyncAnthropic
            self._claude_client = AsyncAnthropic(api_key=settings.claude.api_key, max_retries=0)
        return self._claude_client

    # ------------------------------------------------------------------
    # Core: call the active provider with retry + fallback
    # ------------------------------------------------------------------

    async def _call_provider_with_retry(
        self,
        provider: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        *,
        timeout: float = 120.0,
        retries: int = 2,
    ) -> tuple[str, int]:
        """Call a single provider with timeout and exponential-backoff retry."""
        for attempt in range(retries + 1):
            try:
                return await asyncio.wait_for(
                    self._call_provider(provider, system_prompt, user_prompt, max_tokens),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Provider %s timed out after %.0fs (attempt %d/%d)",
                    provider, timeout, attempt + 1, retries + 1,
                )
                if attempt == retries:
                    raise
            except Exception as e:
                logger.warning(
                    "Provider %s error on attempt %d/%d: %s",
                    provider, attempt + 1, retries + 1, e,
                )
                if attempt == retries:
                    raise
            await asyncio.sleep(2 ** attempt)
        raise RuntimeError("unreachable")

    async def _call_api(self, system_prompt: str, user_prompt: str, max_tokens: int = None) -> tuple[str, int]:
        """Call the structured-output API (returns JSON matching _REVIEW_JSON_SCHEMA)."""
        mt = max_tokens or self._max_tokens
        providers = _get_provider_order()
        last_err = None
        breaker = get_circuit_breaker()

        for provider in providers:
            if provider == "nvidia" and settings.nvidia_accounts:
                # Per-key round robin with skip-unhealthy logic
                accs = settings.nvidia_accounts
                for _ in range(len(accs)):
                    acc = await self._next_nvidia_account()
                    key_label = f"nvidia:{acc.key_id}"
                    if not await breaker.is_available(key_label):
                        continue
                    try:
                        raw, tokens = await self._call_provider_with_retry("nvidia", system_prompt, user_prompt, mt, nvidia_acc=acc)
                        await breaker.record_success(key_label)
                        if provider != providers[0]:
                            logger.info("Used fallback provider: %s", key_label)
                            await breaker.record_fallback_used(key_label)
                        return raw, tokens
                    except Exception as e:
                        status_code = getattr(e, "status_code", None)
                        if status_code is None:
                            response = getattr(e, "response", None)
                            status_code = getattr(response, "status_code", None)
                        is_auth = status_code in (401, 403)
                        await breaker.record_failure(key_label, is_auth_error=is_auth)
                        logger.warning("NVIDIA key %s failed: %s", acc.key_id, e)
                        if is_auth:
                            from config import mark_key_expired
                            mark_key_expired(acc.key_id, acc.api_key)
                        last_err = e
                continue # All NVIDIA keys failed or skipped

            # Standard provider logic (deepseek, openai, claude, gemini)
            if not await breaker.is_available(provider):
                logger.info("Skipping %s — circuit open, using next fallback", provider)
                continue
            try:
                raw, tokens = await self._call_provider_with_retry(provider, system_prompt, user_prompt, mt)
                await breaker.record_success(provider)
                if provider != providers[0]:
                    logger.info("Used fallback provider: %s", provider)
                    await breaker.record_fallback_used(provider)
                return raw, tokens
            except Exception as e:
                response = getattr(e, "response", None)
                status_code: int | None = getattr(response, "status_code", None)
                is_auth = status_code in (401, 403)
                await breaker.record_failure(provider, is_auth_error=is_auth)
                logger.warning("Provider %s failed after retries: %s", provider, e)
                last_err = e
        raise last_err or RuntimeError("All AI providers failed")

    async def _call_api_text(self, system_prompt: str, user_prompt: str, max_tokens: int = 500) -> str:
        """
        Call the API expecting a plain-text response (NOT JSON schema mode).
        Used for synthesis/summary calls that don't need structured output.
        Falls back gracefully if the provider returns JSON anyway.
        """
        providers = _get_provider_order()
        last_err = None
        breaker = get_circuit_breaker()

        for provider in providers:
            if provider == "nvidia" and settings.nvidia_accounts:
                accs = settings.nvidia_accounts
                for _ in range(len(accs)):
                    acc = await self._next_nvidia_account()
                    key_label = f"nvidia:{acc.key_id}"
                    if not await breaker.is_available(key_label): continue
                    try:
                        raw, _ = await self._call_provider_with_retry("nvidia", system_prompt, user_prompt, max_tokens, plain_text=True, nvidia_acc=acc)
                        await breaker.record_success(key_label)
                        if provider != providers[0]: await breaker.record_fallback_used(key_label)
                        return raw.strip()
                    except Exception as e:
                        status_code = getattr(e, "status_code", None)
                        if status_code is None:
                            response = getattr(e, "response", None)
                            status_code = getattr(response, "status_code", None)
                        is_auth = status_code in (401, 403)
                        await breaker.record_failure(key_label, is_auth_error=is_auth)
                        logger.warning("NVIDIA key %s (plain-text) failed: %s", acc.key_id, e)
                        if is_auth:
                            from config import mark_key_expired
                            mark_key_expired(acc.key_id, acc.api_key)
                        last_err = e
                continue

            if not await breaker.is_available(provider):
                logger.info("Skipping %s (plain-text) — circuit open", provider)
                continue
            try:
                raw, _ = await self._call_provider_with_retry(
                    provider, system_prompt, user_prompt, max_tokens,
                    plain_text=True,
                )
                await breaker.record_success(provider)
                if provider != providers[0]:
                    await breaker.record_fallback_used(provider)
                return raw.strip()
            except Exception as e:
                response = getattr(e, "response", None)
                status_code: int | None = getattr(response, "status_code", None)
                is_auth = status_code in (401, 403)
                await breaker.record_failure(provider, is_auth_error=is_auth)
                logger.warning("Provider %s (plain-text) failed: %s", provider, e)
                last_err = e
        raise last_err or RuntimeError("All AI providers failed for plain-text call")

    async def _call_provider(
        self,
        provider: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        plain_text: bool = False,
        nvidia_acc = None,
    ) -> tuple[str, int]:
        if provider == "openai":
            return await self._call_openai(system_prompt, user_prompt, max_tokens, plain_text=plain_text)
        if provider == "claude":
            return await self._call_claude(system_prompt, user_prompt, max_tokens, plain_text=plain_text)
        if provider == "nvidia":
            return await self._call_nvidia(system_prompt, user_prompt, max_tokens, account=nvidia_acc)
        if provider == "gemini":
            return await self._call_gemini(system_prompt, user_prompt, max_tokens, plain_text=plain_text)
        if provider == "deepseek":
            return await self._call_deepseek(system_prompt, user_prompt, max_tokens)
        raise ValueError(f"Unknown provider: {provider}")

    async def _call_provider_with_retry(
        self,
        provider: str,
        system_prompt: str,
        user_prompt: str,
        max_tokens: int,
        *,
        timeout: float = 120.0,
        retries: int = 2,
        plain_text: bool = False,
        nvidia_acc = None,
    ) -> tuple[str, int]:
        """
        Call a single provider with timeout and smart retry logic:

        - 429 Too Many Requests: honours the Retry-After response header
          (falls back to exponential backoff when header is absent).
        - 4xx errors (except 429): fast-fails immediately — retrying a bad
          key / forbidden endpoint will never succeed.
        - Timeout / 5xx / network errors: standard exponential backoff.
        """
        for attempt in range(retries + 1):
            try:
                return await asyncio.wait_for(
                    self._call_provider(provider, system_prompt, user_prompt, max_tokens, plain_text=plain_text, nvidia_acc=nvidia_acc),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Provider %s timed out after %.0fs (attempt %d/%d)",
                    provider, timeout, attempt + 1, retries + 1,
                )
                if attempt == retries:
                    raise
                await asyncio.sleep(2 ** attempt)

            except Exception as e:
                status_code = getattr(e, "status_code", None)
                if status_code is None:
                    response = getattr(e, "response", None)
                    status_code = getattr(response, "status_code", None)

                # ── 4xx that is NOT 429: fast-fail, don't retry ──────────────
                # 403 Forbidden  → key expired / invalid / wrong scope
                # 401 Unauthorized → missing or malformed auth header
                # 400 Bad Request → malformed payload (won't fix itself)
                if status_code and 400 <= status_code < 500 and status_code != 429:
                    logger.warning(
                        "Provider %s returned HTTP %d — skipping retries (auth/request error)",
                        provider, status_code,
                    )
                    if status_code in (401, 403):
                        await get_circuit_breaker().record_auth_error(provider)
                    raise  # bubble up immediately to the fallback chain

                # ── 429 Too Many Requests: honour Retry-After if present ─────
                if status_code == 429:
                    await get_circuit_breaker().record_rate_limit(provider)
                    retry_after_raw = getattr(getattr(response, "headers", {}), "get", lambda k, d=None: d)("retry-after")
                    try:
                        retry_after = float(retry_after_raw) if retry_after_raw else 2 ** attempt
                    except (TypeError, ValueError):
                        retry_after = 2 ** attempt
                    retry_after = max(retry_after, 2 ** attempt)  # never shorter than backoff
                    logger.warning(
                        "Provider %s rate-limited (429) — sleeping %.1fs before attempt %d/%d",
                        provider, retry_after, attempt + 1, retries + 1,
                    )
                    if attempt == retries:
                        raise
                    await asyncio.sleep(retry_after)
                    continue

                # ── Generic error (5xx, network, etc.) ──────────────────────
                logger.warning(
                    "Provider %s error on attempt %d/%d: %s",
                    provider, attempt + 1, retries + 1, e,
                )
                if attempt == retries:
                    raise
                await asyncio.sleep(2 ** attempt)

        raise RuntimeError("unreachable")

    async def _call_openai(
        self, system_prompt: str, user_prompt: str, max_tokens: int, plain_text: bool = False
    ) -> tuple[str, int]:
        """
        OpenAI call.
        - Structured reviews: use native JSON Schema mode (guarantees valid JSON).
        - Plain-text calls (e.g. summary synthesis): standard chat completion.
        """
        client = self._get_openai()
        kwargs = dict(
            model=settings.openai.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=settings.openai.temperature,
        )
        if not plain_text:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "code_review",
                    "strict": True,
                    "schema": _REVIEW_JSON_SCHEMA,
                },
            }
        response = await client.chat.completions.create(**kwargs)
        raw = response.choices[0].message.content or "{}"
        tokens = response.usage.total_tokens if response.usage else 0
        return raw, tokens

    async def _call_claude(
        self, system_prompt: str, user_prompt: str, max_tokens: int, plain_text: bool = False
    ) -> tuple[str, int]:
        """
        Claude call.
        - Structured reviews: assistant JSON prefill to steer toward valid JSON.
        - Plain-text calls: no prefill.
        """
        client = self._get_claude()
        messages = [{"role": "user", "content": user_prompt}]
        if not plain_text:
            messages.append({"role": "assistant", "content": "{"})  # prefill forces JSON start

        response = await client.messages.create(
            model=settings.claude.model,
            max_tokens=max_tokens,
            temperature=settings.claude.temperature,
            system=system_prompt,
            messages=messages,
        )
        raw = response.content[0].text if response.content else ""
        if not plain_text:
            raw = "{" + raw   # re-attach the prefill character
        tokens = (response.usage.input_tokens + response.usage.output_tokens) if response.usage else 0
        return raw, tokens

    async def _call_nvidia(self, system_prompt: str, user_prompt: str, max_tokens: int, account=None) -> tuple[str, int]:
        """NVIDIA NIM API (OpenAI-compatible, regex fallback for JSON)."""
        from openai import AsyncOpenAI
        nv = account or await self._next_nvidia_account()
        client = AsyncOpenAI(
            api_key=nv.api_key,
            base_url=nv.base_url,
            max_retries=0,
        )
        resp = await client.chat.completions.create(
            model=nv.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=nv.temperature,
        )
        self._model = f"nvidia/{nv.model}"
        raw = resp.choices[0].message.content or ""
        tokens = resp.usage.total_tokens if resp.usage else 0
        return raw, tokens

    async def _call_deepseek(self, system_prompt: str, user_prompt: str, max_tokens: int) -> tuple[str, int]:
        """DeepSeek API (OpenAI-compatible). Uses deepseek-coder by default."""
        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            api_key=settings.deepseek.api_key,
            base_url=settings.deepseek.base_url,
            max_retries=0,
        )
        resp = await client.chat.completions.create(
            model=settings.deepseek.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user",   "content": user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=settings.deepseek.temperature,
        )
        self._model = f"deepseek/{settings.deepseek.model}"
        raw = resp.choices[0].message.content or ""
        tokens = resp.usage.total_tokens if resp.usage else 0
        return raw, tokens

    async def _call_gemini(
        self, system_prompt: str, user_prompt: str, max_tokens: int, plain_text: bool = False
    ) -> tuple[str, int]:
        """
        Gemini call.
        - Structured reviews: response_mime_type JSON to enforce valid JSON output.
        - Plain-text calls: default mime type.
        """
        import google.generativeai as genai
        genai.configure(api_key=settings.gemini.api_key)
        model = genai.GenerativeModel(
            model_name=settings.gemini.model,
            system_instruction=system_prompt,
        )
        gen_cfg = dict(
            max_output_tokens=max_tokens,
            temperature=settings.gemini.temperature,
        )
        if not plain_text:
            gen_cfg["response_mime_type"] = "application/json"
        response = await model.generate_content_async(
            user_prompt,
            generation_config=genai.GenerationConfig(**gen_cfg),
        )
        raw = response.text if hasattr(response, "text") else "{}"
        return raw, 0

    # ------------------------------------------------------------------
    # Public: review a full file
    # ------------------------------------------------------------------

    async def review_file(
        self,
        code: str,
        filename: str,
        language: Optional[str] = None,
        context: Optional[str] = None,
        focus_areas: Optional[List[IssueType]] = None,
        file_summaries: Optional[List[dict]] = None,
        use_cache: bool = True,
    ) -> FileReview:
        cache_key = self._cache.make_key(filename, code, _active_model())
        if use_cache:
            try:
                cached = await self._cache.get(cache_key)
                if cached:
                    logger.info("Cache hit for %s", filename)
                    cached["provider"] = "cache"
                    cached["time_taken"] = 0.0
                    return FileReview(**cached)
            except Exception as e:
                logger.warning("Cache GET failed for %s, bypassing cache: %s", filename, e)

        cross_file_context = ""
        file_start_time = time.monotonic()
        if file_summaries:
            cross_file_context = build_multi_file_context_prompt(file_summaries)

        system_tokens = count_tokens(SYSTEM_PROMPT)
        overhead = system_tokens + 400 + count_tokens(cross_file_context)
        max_code_tokens = max(1500, 7500 - overhead)

        chunks = chunk_code(code, max_tokens_per_chunk=max_code_tokens)
        total_lines = len(code.splitlines())
        was_chunked = len(chunks) > 1

        if was_chunked:
            logger.info("File %s split into %d chunks", filename, len(chunks))

        all_issues: List[CodeIssue] = []
        chunk_summaries: List[str] = []
        all_test_cases: List[str] = []
        total_tokens = 0

        for chunk in chunks:
            issues, summary, tokens, test_cases = await self._review_chunk(
                chunk=chunk,
                filename=filename,
                language=language,
                context=context,
                focus_areas=focus_areas,
                cross_file_context=cross_file_context,
            )
            all_issues.extend(issues)
            chunk_summaries.append(summary)
            if test_cases:
                all_test_cases.extend(test_cases)
            total_tokens += tokens

        all_issues = _deduplicate_issues(all_issues)
        all_test_cases = list(dict.fromkeys(all_test_cases))  # deduplicate strings
        score = _compute_score(all_issues)

        if len(chunk_summaries) == 1:
            overall_summary = chunk_summaries[0]
        else:
            overall_summary = await self._synthesize_summary(chunk_summaries, filename, all_issues)

        file_review = FileReview(
            filename=filename,
            language=language or _detect_language(filename),
            total_lines=total_lines,
            issues=all_issues,
            test_cases=all_test_cases,
            summary=overall_summary,
            score=score,
            was_chunked=was_chunked,
            chunks_processed=len(chunks),
            provider=_active_provider(),
            time_taken=round(time.monotonic() - file_start_time, 2)
        )

        if use_cache:
            try:
                await self._cache.set(cache_key, file_review.model_dump())
            except Exception as e:
                logger.warning("Cache SET failed for %s: %s", filename, e)

        return file_review

    # ------------------------------------------------------------------
    # Public: review a diff
    # ------------------------------------------------------------------

    async def review_diff(
        self,
        diff: str,
        filename: str,
        pr_title: Optional[str] = None,
        pr_description: Optional[str] = None,
        context: Optional[str] = None,
        focus_areas: Optional[List[IssueType]] = None,
    ) -> FileReview:
        prompt = build_diff_prompt(
            diff=diff, filename=filename, pr_title=pr_title,
            pr_description=pr_description, context=context, focus_areas=focus_areas,
        )
        estimated_tokens = count_tokens(DIFF_SYSTEM_PROMPT) + count_tokens(prompt) + self._max_tokens
        await get_rate_limiter(_active_provider()).acquire(estimated_tokens)

        start = time.monotonic()
        raw, tokens_used = await self._call_api(DIFF_SYSTEM_PROMPT, prompt)
        logger.info("Diff review for %s took %.2fs", filename, time.monotonic() - start)

        parsed = _parse_review_json(raw)
        issues = []
        for i in parsed.get("issues", []):
            try:
                issues.append(CodeIssue(**i))
            except Exception as e:
                logger.warning("Skipping malformed issue: %s", e)

        return FileReview(
            filename=filename,
            language=_detect_language(filename),
            issues=issues,
            test_cases=parsed.get("test_cases", []),
            summary=parsed.get("summary", ""),
            score=parsed.get("score", _compute_score(issues)),
            was_chunked=False,
            chunks_processed=1,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _review_chunk(
        self,
        chunk: CodeChunk,
        filename: str,
        language: Optional[str],
        context: Optional[str],
        focus_areas: Optional[List[IssueType]],
        cross_file_context: str,
    ) -> tuple[List[CodeIssue], str, int, List[str]]:
        numbered_code = add_line_numbers_with_offset(chunk.content, chunk.start_line)
        user_prompt = ""
        if cross_file_context:
            user_prompt += cross_file_context + "\n\n"
        user_prompt += build_review_prompt(
            code=numbered_code, filename=filename, language=language,
            context=context, focus_areas=focus_areas,
            chunk_info=chunk.info_string if chunk.total_chunks > 1 else None,
        )

        estimated_tokens = count_tokens(SYSTEM_PROMPT) + count_tokens(user_prompt) + self._max_tokens
        await get_rate_limiter(_active_provider()).acquire(estimated_tokens)

        start = time.monotonic()
        raw, tokens_used = await self._call_api(SYSTEM_PROMPT, user_prompt)
        logger.debug("Chunk %d/%d for %s: %.2fs", chunk.chunk_index + 1, chunk.total_chunks, filename, time.monotonic() - start)

        parsed = _parse_review_json(raw)
        issues = []
        for issue_dict in parsed.get("issues", []):
            try:
                issues.append(CodeIssue(**issue_dict))
            except Exception as e:
                logger.warning("Skipping malformed issue: %s -- %s", issue_dict, e)

        return issues, parsed.get("summary", ""), tokens_used, parsed.get("test_cases", [])

    async def _synthesize_summary(self, chunk_summaries: List[str], filename: str, issues: List[CodeIssue]) -> str:
        """
        Merge per-chunk summaries into one paragraph.
        Uses plain-text mode -- NOT structured JSON -- so providers don't wrap the
        answer in a JSON object.  If the provider returns JSON anyway (e.g. because
        of a sticky system-level instruction), we extract the "summary" field.
        """
        combined = "\n\n".join(f"Chunk {i+1} summary: {s}" for i, s in enumerate(chunk_summaries))
        issue_counts = Counter(i.severity.value for i in issues)
        prompt = (
            f"You reviewed `{filename}` in {len(chunk_summaries)} chunks. "
            f"Merge the following chunk summaries into ONE concise paragraph (3-5 sentences). "
            f"Issue count: {dict(issue_counts)}.\n\n{combined}\n\n"
            f"Return ONLY the merged summary paragraph, no JSON, no headings."
        )
        await get_rate_limiter(_active_provider()).acquire(count_tokens(prompt) + 300)
        raw = await self._call_api_text("You are a concise technical writer.", prompt, max_tokens=300)
        # Guard: if the provider wrapped in JSON despite plain-text mode, unwrap it
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "summary" in parsed:
                return str(parsed["summary"])
        except (json.JSONDecodeError, TypeError):
            pass
        return raw


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _parse_review_json(raw: str) -> dict:
    """
    Parse JSON from a model response.

    Providers using structured outputs (OpenAI json_schema, Gemini response_mime_type)
    return clean JSON. For providers without native structured outputs (NVIDIA, Claude
    without tool-use), we strip markdown fences and fall back to a JSON object search.
    """
    raw = raw.strip()
    # Strip markdown fences if present
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Last resort: find the outermost JSON object
    match = re.search(r"\{[\s\S]*\}", raw)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            pass
    logger.warning("Could not parse model output as JSON. Raw (first 300 chars): %s", raw[:300])
    return {"summary": "Could not parse model response.", "issues": []}


def _deduplicate_issues(issues: List[CodeIssue]) -> List[CodeIssue]:
    seen = set()
    unique = []
    for issue in issues:
        key = (issue.line, issue.type, issue.severity)
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return unique


def _compute_score(issues: List[CodeIssue]) -> int:
    """
    Score 0-100 with per-severity caps (diminishing returns).

        critical: -25 each, capped at -70
        high:     -10 each, capped at -40
        medium:   - 3 each, capped at -18
        low:      - 1 each, capped at - 8
    """
    sev = Counter(i.severity.value for i in issues)

    def capped(count: int, per_issue: int, cap: int) -> int:
        return min(count * per_issue, cap)

    penalty = (
        capped(sev.get("critical", 0), 25, 70) +
        capped(sev.get("high",     0), 10, 40) +
        capped(sev.get("medium",   0),  3, 18) +
        capped(sev.get("low",      0),  1,  8)
    )
    return max(0, 100 - penalty)


def _detect_language(filename: str) -> Optional[str]:
    from pathlib import Path
    ext = Path(filename).suffix.lower()
    mapping = {
        ".py": "python", ".js": "javascript", ".ts": "typescript",
        ".tsx": "typescript", ".jsx": "javascript", ".java": "java",
        ".go": "go", ".rs": "rust", ".cpp": "cpp", ".c": "c",
        ".cs": "csharp", ".rb": "ruby", ".php": "php",
    }
    return mapping.get(ext)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_service: LLMService | None = None


def get_llm_service() -> LLMService:
    global _service
    if _service is None:
        _service = LLMService()
    return _service


# Backward-compatible alias -- remove once all call sites are updated
def get_openai_service() -> LLMService:
    return get_llm_service()

# --- END OF FILE: llm_service.py ---
