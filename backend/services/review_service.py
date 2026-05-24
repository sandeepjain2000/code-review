"""
review_service.py — Orchestrates all review workflows.

This is the brain: it accepts high-level requests, coordinates between
the OpenAI service, GitHub service, scanner service, and cache,
then assembles ReviewResult objects.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import List, Optional

from models.schemas import (
    FileReview,
    FolderScanRequest,
    GitHubPRReviewRequest,
    InputType,
    IssueType,
    ReviewResult,
    ReviewStatus,
    SnippetReviewRequest,
)
from services.cache_service import get_cache
from services.github_service import get_github_service
from services.llm_service import get_llm_service
from services.scanner_service import get_scanner_service

logger = logging.getLogger(__name__)


class ReviewService:
    """Top-level review orchestrator."""

    def __init__(self):
        self._ai = get_llm_service()
        self._github = get_github_service()
        self._scanner = get_scanner_service()
        self._cache = get_cache()

    # ------------------------------------------------------------------
    # 1. Snippet / pasted code review
    # ------------------------------------------------------------------

    async def review_snippet(self, request: SnippetReviewRequest) -> ReviewResult:
        review_id = _new_id()
        logger.info("Snippet review %s: %s", review_id, request.filename)

        logger.info("Reviewing %s...", request.filename)
        file_review = await self._ai.review_file(
            code=request.code,
            filename=request.filename,
            language=request.language,
            context=request.context,
            focus_areas=request.focus_areas,
        )
        logger.info("Reviewed %s -- %d issue(s)", request.filename, len(file_review.issues))

        return ReviewResult(
            review_id=review_id,
            input_type=InputType.SNIPPET,
            status=ReviewStatus.COMPLETED,
            files=[file_review],
            overall_summary=file_review.summary,
            model_used=self._ai._model,
        )

    # ------------------------------------------------------------------
    # 2. GitHub PR review
    # ------------------------------------------------------------------

    async def review_github_pr(self, request: GitHubPRReviewRequest) -> ReviewResult:
        review_id = _new_id()
        logger.info(
            "PR review %s: %s/%s#%d (diff_only=%s)",
            review_id, request.owner, request.repo, request.pull_number, request.diff_only,
        )

        # --- Check PR-level cache ---
        cache_key = self._cache.make_pr_key(
            request.owner, request.repo, request.pull_number, self._ai._model
        )
        cached = await self._cache.get(cache_key)
        if cached:
            logger.info("Cache hit for PR %s/%s#%d", request.owner, request.repo, request.pull_number)
            result = ReviewResult(**cached)
            result.cached = True
            return result

        # --- Fetch PR data from GitHub ---
        pr_meta, pr_files = await self._github.get_pr_with_files(
            owner=request.owner,
            repo=request.repo,
            pull_number=request.pull_number,
            diff_only=request.diff_only,
        )

        if not pr_files:
            return ReviewResult(
                review_id=review_id,
                input_type=InputType.GITHUB_PR,
                status=ReviewStatus.COMPLETED,
                overall_summary="No reviewable files found in this PR.",
                model_used=self._ai._model,
            )

        pr_title = pr_meta.get("title", "")
        pr_body = pr_meta.get("body", "") or ""

        # --- Review each file (concurrently with cap) ---
        file_reviews = await self._review_files_concurrently(
            files=pr_files,
            pr_title=pr_title,
            pr_description=pr_body,
            context=request.context,
            focus_areas=request.focus_areas,
            diff_only=request.diff_only,
        )

        overall_summary = _build_overall_summary(file_reviews, pr_title)

        result = ReviewResult(
            review_id=review_id,
            input_type=InputType.GITHUB_PR,
            status=ReviewStatus.COMPLETED,
            files=file_reviews,
            overall_summary=overall_summary,
            model_used=self._ai._model,
        )

        await self._cache.set(cache_key, result.model_dump())

        # --- Optionally post back to GitHub ---
        if settings_allow_post():
            await self._post_github_review(pr_meta, result, request)

        return result

    # ------------------------------------------------------------------
    # 3. Folder scan review
    # ------------------------------------------------------------------

    async def review_folder(self, request: FolderScanRequest) -> ReviewResult:
        review_id = _new_id()
        logger.info("Folder review %s: %s", review_id, request.folder_path)

        source_files = get_scanner_service().scan(
            folder_path=request.folder_path,
            recursive=request.recursive,
            max_files=request.max_files,
        )

        if not source_files:
            return ReviewResult(
                review_id=review_id,
                input_type=InputType.FOLDER_SCAN,
                status=ReviewStatus.COMPLETED,
                overall_summary="No reviewable source files found in the specified folder.",
                model_used=self._ai._model,
            )

        # Process files sequentially — each file awaits the OpenAI call so the
        # server stays non-blocking. Sequential order also lets us pass growing
        # cross-file context to each subsequent review.
        file_reviews: List[FileReview] = []
        file_summaries: List[dict] = []
        consecutive_failures = 0
        max_failures = __import__("config").settings.scanner.max_consecutive_failures
        status = ReviewStatus.COMPLETED
        error_msg = None

        for sf in source_files:
            try:
                logger.info("Reviewing %s...", sf.relative_path)
                review = await self._ai.review_file(
                    code=sf.content,
                    filename=sf.relative_path,
                    language=sf.language,
                    context=request.context,
                    focus_areas=request.focus_areas,
                    file_summaries=list(file_summaries),
                )
                consecutive_failures = 0  # reset on success
                file_summaries.append({"filename": sf.relative_path, "summary": review.summary})
                file_reviews.append(review)
                logger.info("Reviewed %s -- %d issue(s)", sf.relative_path, len(review.issues))
            except Exception as e:
                consecutive_failures += 1
                logger.error(
                    "File review failed (%d/%d consecutive): %s -- %s",
                    consecutive_failures, max_failures, sf.relative_path, e, exc_info=True,
                )
                file_reviews.append(FileReview(
                    filename=sf.relative_path,
                    summary=f"Review failed: {e}",
                ))
                if consecutive_failures >= max_failures:
                    error_msg = f"Scan aborted: {consecutive_failures} consecutive file failures. Last error: {e}"
                    logger.error(error_msg)
                    status = ReviewStatus.FAILED
                    break

        overall_summary = _build_overall_summary(file_reviews, folder=request.folder_path) if file_reviews else "No files reviewed."

        return ReviewResult(
            review_id=review_id,
            input_type=InputType.FOLDER_SCAN,
            status=status,
            files=list(file_reviews),
            overall_summary=overall_summary,
            model_used=self._ai._model,
            error=error_msg,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _review_files_concurrently(
        self,
        files: List[dict],
        pr_title: str,
        pr_description: str,
        context: Optional[str],
        focus_areas: Optional[List[IssueType]],
        diff_only: bool,
        max_concurrent: int = 4,
    ) -> List[FileReview]:
        semaphore = asyncio.Semaphore(max_concurrent)
        results = []

        async def review_one(f: dict):
            async with semaphore:
                filename = f["filename"]
                diff = f.get("diff", "")
                content = f.get("content", "")

                logger.info("Reviewing %s...", filename)
                if diff_only and diff:
                    res = await self._ai.review_diff(
                        diff=diff,
                        filename=filename,
                        pr_title=pr_title,
                        pr_description=pr_description,
                        context=context,
                        focus_areas=focus_areas,
                    )
                elif content:
                    res = await self._ai.review_file(
                        code=content,
                        filename=filename,
                        context=context,
                        focus_areas=focus_areas,
                    )
                else:
                    # No diff and no content (e.g. binary file)
                    res = FileReview(
                        filename=filename,
                        summary="Skipped: no reviewable content.",
                    )
                logger.info("Reviewed %s -- %d issue(s)", filename, len(res.issues) if res.issues else 0)
                return res

        tasks = [review_one(f) for f in files]
        results = await asyncio.gather(*tasks)
        return list(results)

    async def _post_github_review(
        self, pr_meta: dict, result: ReviewResult, request: GitHubPRReviewRequest
    ) -> None:
        """Post review summary + inline comments back to GitHub."""
        try:
            commit_sha = pr_meta["head"]["sha"]

            # Build inline comments
            comments = []
            for file_review in result.files:
                for issue in file_review.issues:
                    if issue.line:
                        comments.append({
                            "path": file_review.filename,
                            "line": issue.line,
                            "body": self._github.format_review_comment(issue),
                        })

            # Summary body
            body = self._github.format_pr_summary(result)

            # Only REQUEST_CHANGES if there are high/critical issues
            has_blocking = any(
                i.severity.value in ("high", "critical")
                for f in result.files
                for i in f.issues
            )
            event = "REQUEST_CHANGES" if has_blocking else "COMMENT"

            await self._github.create_review(
                owner=request.owner,
                repo=request.repo,
                pull_number=request.pull_number,
                commit_sha=commit_sha,
                comments=comments[:50],  # GitHub API cap per review
                body=body,
                event=event,
            )
            logger.info("Posted GitHub review to %s/%s#%d", request.owner, request.repo, request.pull_number)

        except Exception as e:
            logger.error("Failed to post GitHub review: %s", e)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _new_id() -> str:
    return uuid.uuid4().hex


def _build_overall_summary(
    file_reviews: List[FileReview],
    pr_title: str = "",
    folder: str = "",
    coverage: int = 100,
) -> str:
    """Build a rich project-level intelligence summary with action plan."""
    from collections import Counter, defaultdict

    total_files = len(file_reviews)
    all_issues = [i for f in file_reviews for i in f.issues]
    total_issues = len(all_issues)
    
    api_counts = defaultdict(int)
    api_times = defaultdict(list)
    for f in file_reviews:
        p = getattr(f, "provider", "") or "unknown"
        api_counts[p] += 1
        t = getattr(f, "time_taken", 0.0)
        if t > 0:
            api_times[p].append(t)

    # ── Severity counts ──────────────────────────────────────────────────────
    sev = Counter(i.severity.value for i in all_issues)
    n_critical = sev.get("critical", 0)
    n_high     = sev.get("high", 0)
    n_medium   = sev.get("medium", 0)
    n_low      = sev.get("low", 0)

    # ── Issue type counts (total occurrences) ────────────────────────────────
    type_counts = Counter(i.type.value for i in all_issues)

    # ── Repeated patterns: same issue type in how many files ─────────────────
    type_file_counts: Counter = Counter()
    for f in file_reviews:
        seen: set = set()
        for i in f.issues:
            t = i.type.value
            if t not in seen:
                type_file_counts[t] += 1
                seen.add(t)

    # ── Scores ───────────────────────────────────────────────────────────────
    scores = [f.score for f in file_reviews if f.score is not None]
    avg_score = round(sum(scores) / len(scores)) if scores else None

    # ── Security risk score (0-100, inverted) ────────────────────────────────
    sec_types = {"security", "multi_tenancy", "gdpr", "authentication", "authorization"}
    sec_issues = [i for i in all_issues if i.type.value in sec_types]
    sec_critical = sum(1 for i in sec_issues if i.severity.value == "critical")
    sec_high     = sum(1 for i in sec_issues if i.severity.value == "high")
    sec_penalty  = min(100, sec_critical * 20 + sec_high * 8 + len(sec_issues) * 2)
    security_score = max(0, 100 - sec_penalty)

    # ── Production readiness verdict ─────────────────────────────────────────
    if n_critical > 0:
        verdict = "🚨 NOT production-ready — critical issues must be resolved first"
        prod_ready = False
    elif n_high > 10:
        verdict = "🔴 NOT production-ready — too many high-severity issues"
        prod_ready = False
    elif n_high > 4:
        verdict = "⚠️  Significant work needed before production deployment"
        prod_ready = False
    elif n_high > 0:
        verdict = "⚠️  Minor fixes needed — review high-severity issues before deploy"
        prod_ready = False
    else:
        verdict = "✅ Production-ready — no critical or high-severity issues found"
        prod_ready = True

    # ── Top 5 most critical issues ───────────────────────────────────────────
    sev_order = {"critical": 0, "high": 1, "medium": 2, "low": 3}
    top_issues = sorted(
        all_issues,
        key=lambda i: (sev_order.get(i.severity.value, 4), 0)
    )[:5]

    # ── Files with most issues ───────────────────────────────────────────────
    hot_files = sorted(
        [(f.filename, len(f.issues), f.score) for f in file_reviews if f.issues],
        key=lambda x: -x[1]
    )[:5]

    # ── Action plan ──────────────────────────────────────────────────────────
    action_days: list = []

    # Day 0 — Infra fix for partial analysis
    if coverage < 100:
        action_days.append(("DAY 0 — Infra Fix", [
            "Resolve scanner errors (e.g. review_cache DB issue)",
            "Re-run full scan for accurate results"
        ]))

    # Day 1 — blockers
    d1 = []
    if n_critical > 0:
        d1.append(f"Fix {n_critical} critical issue(s) — immediate blockers")
    for t, fc in type_file_counts.most_common():
        if fc >= 3 and t in sec_types:
            d1.append(f"Resolve {type_counts[t]} {t.replace('_',' ')} issue(s) across {fc} files")
    if d1:
        action_days.append(("DAY 1 — Blockers", d1[:4]))

    # Day 2 — high severity
    d2 = []
    if n_high > 0:
        high_by_type = Counter(
            i.type.value for i in all_issues if i.severity.value == "high"
        )
        for t, cnt in high_by_type.most_common(4):
            d2.append(f"Fix {cnt} high-severity {t.replace('_',' ')} issue(s)")
    if d2:
        action_days.append(("DAY 2 — High Priority", d2))

    # Day 3 — patterns & medium
    d3 = []
    for t, fc in type_file_counts.most_common():
        if fc >= 4 and t not in sec_types:
            d3.append(
                f"Refactor repeated {t.replace('_',' ')} pattern ({fc} files) → extract shared helper"
            )
    if n_medium > 5:
        d3.append(f"Address {n_medium} medium-severity issues (reliability/maintainability)")
    if d3:
        action_days.append(("DAY 3 — Patterns & Medium", d3[:4]))

    # ── Build the text ────────────────────────────────────────────────────────
    sep = "=" * 65
    thin = "-" * 65
    lines: list = [
        sep,
        "  PROJECT INTELLIGENCE REPORT — CodeSentinel",
        sep,
    ]

    folder_str = str(folder)
    context_label = pr_title or folder_str.replace("\\", "/").rstrip("/").split("/")[-1] or "codebase"
    lines += [
        f"  Scope   : {context_label}",
        f"  Files   : {total_files}   Issues: {total_issues}   Avg Score: {avg_score}/100" if avg_score else
        f"  Files   : {total_files}   Issues: {total_issues}",
    ]

    if coverage < 100:
        lines.append(f"  Coverage: {coverage}% (Partial Analysis — Unanalyzed files are high risk)")

    lines += [
        "",
        f"  PROJECT STATUS: {verdict}",
        f"  Security Risk Score: {security_score}/100  (higher = safer)",
        "",
    ]

    if api_counts:
        lines += [thin, "  EXECUTION METRICS", thin]
        for p, count in api_counts.items():
            if p == "cache":
                lines.append(f"  {p.upper():<12} : {count} file(s)  (Instant)")
            else:
                avg_time = sum(api_times[p]) / len(api_times[p]) if api_times.get(p) else 0.0
                lines.append(f"  {p.upper():<12} : {count} file(s)  (Avg time: {avg_time:.2f}s per call)")
        lines.append("")

    # Severity dashboard
    lines += [
        thin,
        "  ISSUE BREAKDOWN",
        thin,
        f"  🔴 Critical : {n_critical}",
        f"  🟠 High     : {n_high}",
        f"  🟡 Medium   : {n_medium}",
        f"  🔵 Low      : {n_low}",
        "",
    ]

    # Top issue types
    if type_counts:
        lines += [thin, "  TOP ISSUE CATEGORIES", thin]
        for t, cnt in type_counts.most_common(8):
            fc = type_file_counts.get(t, 1)
            lines.append(f"  {t.replace('_',' ').title():<30} {cnt:>3} occurrences  in {fc} file(s)")
        lines.append("")

    # Repeated patterns
    repeated = [(t, fc) for t, fc in type_file_counts.most_common() if fc >= 3]
    if repeated:
        lines += [thin, "  REPEATED PATTERNS (architectural, not just file-level)", thin]
        for t, fc in repeated[:6]:
            cnt = type_counts[t]
            lines.append(f"  • {t.replace('_',' ').title()} appears in {fc} files ({cnt} total occurrences)")
        lines += [
            "",
            "  → These are architectural weaknesses — fix the root cause, not each instance.",
            "",
        ]

    # Top 5 critical fixes
    if top_issues:
        lines += [thin, "  TOP 5 FIXES (highest severity first)", thin]
        for idx, issue in enumerate(top_issues, 1):
            fname = next((f.filename for f in file_reviews if issue in f.issues), "")
            loc = f"L{issue.line}" if issue.line else "file-level"
            sev_tag = issue.severity.value.upper()
            lines.append(f"  {idx}. [{sev_tag}] {fname} ({loc})")
            lines.append(f"     {issue.message[:120]}")
            if issue.suggestion:
                lines.append(f"     Fix: {issue.suggestion[:100]}")
            lines.append("")

    # Unanalyzed Files
    failed_files = [f.filename for f in file_reviews if f.score is None and not getattr(f, 'is_round2', False)]
    if failed_files:
        lines += [thin, "  UNANALYZED FILES (HIGH RISK)", thin]
        for fname in failed_files[:10]:
            lines.append(f"  • {fname}")
        if len(failed_files) > 10:
            lines.append(f"  ... and {len(failed_files) - 10} more.")
        lines.append("")

    # Round 2 files
    round2_files = [f for f in file_reviews if getattr(f, 'is_round2', False)]
    if round2_files:
        lines += [thin, "  ROUND 2 FILES (processed after main scan — slow AI responses)", thin]
        for f in round2_files:
            tag = f"score {f.score}/100" if f.score is not None else "failed"
            lines.append(f"  • {f.filename}  ({tag}, {len(f.issues)} issue(s), took >{f.time_taken:.0f}s)")
        lines.append("")

    # Hotspot files
    if hot_files:
        lines += [thin, "  HOTSPOT FILES (most issues)", thin]
        for fname, cnt, score in hot_files:
            score_str = f"  score {score}/100" if score else ""
            lines.append(f"  • {fname}  —  {cnt} issue(s){score_str}")
        lines.append("")

    # Action plan
    if action_days:
        lines += [thin, "  ACTION PLAN", thin]
        for day_label, items in action_days:
            lines.append(f"  {day_label}:")
            for item in items:
                lines.append(f"    - {item}")
            lines.append("")

    lines += [sep, "  END OF PROJECT SUMMARY"]
    lines.append(f"  This report is based on a {'FULL' if coverage == 100 else f'PARTIAL ({coverage}%)'} execution of the scanning script.")
    lines.append(sep)
    return chr(10).join(lines)


def settings_allow_post() -> bool:
    """Only post back to GitHub if a token is configured."""
    return bool(getattr(__import__("config").settings.github, "token", None))


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_review_service: ReviewService | None = None


def get_review_service() -> ReviewService:
    global _review_service
    if _review_service is None:
        _review_service = ReviewService()
    return _review_service
# --- END OF FILE: review_service.py ---
