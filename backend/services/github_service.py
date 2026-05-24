"""
github_service.py -- GitHub API integration.

Features:
- Fetch PR metadata, file list, and diffs
- Post inline review comments back to the PR
- Verify webhook HMAC signatures
- Support both diff-only and full-file review modes
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
from typing import Dict, List, Optional, Tuple

import httpx

from config import settings

logger = logging.getLogger(__name__)

GITHUB_API = "https://api.github.com"


class GitHubService:
    """Wraps the GitHub REST API for PR review workflows."""

    def __init__(self):
        token = settings.github.token
        self._headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"

    # ------------------------------------------------------------------
    # Pull Request data
    # ------------------------------------------------------------------

    async def _get(self, client: httpx.AsyncClient, url: str, **kwargs) -> httpx.Response:
        """GET with automatic rate-limit retry (403/429 + Retry-After)."""
        for attempt in range(3):
            resp = await client.get(url, **kwargs)
            if resp.status_code in (403, 429):
                retry_after = int(resp.headers.get("Retry-After", 60))
                logger.warning(
                    "GitHub rate limit hit (%d) -- waiting %ds (attempt %d/3)",
                    resp.status_code, retry_after, attempt + 1,
                )
                await asyncio.sleep(retry_after)
                continue
            resp.raise_for_status()
            return resp
        resp.raise_for_status()
        return resp

    async def get_pr_metadata(self, owner: str, repo: str, pull_number: int) -> dict:
        """Fetch PR title, body, author, base/head SHAs."""
        url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pull_number}"
        async with httpx.AsyncClient(headers=self._headers, timeout=30) as client:
            resp = await self._get(client, url)
            return resp.json()

    async def get_pr_files(self, owner: str, repo: str, pull_number: int) -> List[dict]:
        """
        Return list of changed files.
        Each entry has: filename, status, additions, deletions, patch (diff).
        """
        url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pull_number}/files"
        files = []
        page = 1

        async with httpx.AsyncClient(headers=self._headers, timeout=30) as client:
            while True:
                resp = await self._get(client, url, params={"per_page": 100, "page": page})
                batch = resp.json()
                if not batch:
                    break
                files.extend(batch)
                if len(batch) < 100:
                    break
                page += 1

        return files

    async def get_file_content(self, owner: str, repo: str, path: str, ref: str) -> Optional[str]:
        """Fetch the full content of a file at a specific commit."""
        url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
        async with httpx.AsyncClient(headers=self._headers, timeout=30) as client:
            resp = await client.get(url, params={"ref": ref})
            if resp.status_code == 404:
                return None
            if resp.status_code in (403, 429):
                retry_after = int(resp.headers.get("Retry-After", 60))
                logger.warning("GitHub rate limit fetching file content -- waiting %ds", retry_after)
                await asyncio.sleep(retry_after)
                resp = await client.get(url, params={"ref": ref})
            resp.raise_for_status()
            data = resp.json()

        import base64
        encoded = data.get("content", "")
        return base64.b64decode(encoded).decode("utf-8", errors="replace")

    async def get_pr_with_files(
        self, owner: str, repo: str, pull_number: int, diff_only: bool = True
    ) -> Tuple[dict, List[Dict[str, str]]]:
        """
        High-level: fetch PR metadata + all changed files.

        Returns:
            (pr_metadata, file_list)
            Each file_list entry: {filename, diff, content (if diff_only=False)}
        """
        pr = await self.get_pr_metadata(owner, repo, pull_number)
        raw_files = await self.get_pr_files(owner, repo, pull_number)

        reviewed_files = []
        for f in raw_files:
            if f.get("status") == "removed":
                continue

            filename = f["filename"]
            diff = f.get("patch", "")

            entry = {"filename": filename, "diff": diff, "status": f.get("status", "modified")}

            if not diff_only and diff:
                head_sha = pr["head"]["sha"]
                content = await self.get_file_content(owner, repo, filename, head_sha)
                entry["content"] = content or ""

            reviewed_files.append(entry)

        return pr, reviewed_files

    # ------------------------------------------------------------------
    # Posting review comments
    # ------------------------------------------------------------------

    async def create_review(
        self,
        owner: str,
        repo: str,
        pull_number: int,
        commit_sha: str,
        comments: List[Dict],
        body: str = "",
        event: str = "COMMENT",
    ) -> dict:
        """
        Post a review with inline comments to the PR.

        comments format: [{"path": "file.py", "line": 23, "body": "..."}]
        event: "COMMENT" | "REQUEST_CHANGES" | "APPROVE"
        """
        url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pull_number}/reviews"
        payload = {
            "commit_id": commit_sha,
            "body": body,
            "event": event,
            "comments": comments,
        }

        async with httpx.AsyncClient(headers=self._headers, timeout=30) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 422:
                logger.warning("GitHub review validation error: %s", resp.text)
            resp.raise_for_status()
            return resp.json()

    async def post_pr_comment(self, owner: str, repo: str, pull_number: int, body: str) -> dict:
        """Post a general (non-inline) comment on the PR."""
        url = f"{GITHUB_API}/repos/{owner}/{repo}/issues/{pull_number}/comments"
        async with httpx.AsyncClient(headers=self._headers, timeout=30) as client:
            resp = await client.post(url, json={"body": body})
            resp.raise_for_status()
            return resp.json()

    # ------------------------------------------------------------------
    # Webhook helpers
    # ------------------------------------------------------------------

    @staticmethod
    def verify_webhook_signature(payload_bytes: bytes, signature_header: str) -> bool:
        """
        Verify the X-Hub-Signature-256 header from GitHub.

        SECURITY: if no webhook secret is configured the request is REJECTED
        by default. To accept unsigned webhooks in local dev only, set
        ALLOW_UNSIGNED_WEBHOOKS=true explicitly.
        """
        secret = settings.github.webhook_secret
        if not secret:
            import os
            if os.environ.get("ALLOW_UNSIGNED_WEBHOOKS", "").lower() == "true":
                logger.warning(
                    "ALLOW_UNSIGNED_WEBHOOKS=true -- accepting without signature. "
                    "Do NOT use this in production."
                )
                return True
            logger.error(
                "Webhook received but GITHUB_WEBHOOK_SECRET is not configured. "
                "Set the secret or enable ALLOW_UNSIGNED_WEBHOOKS=true for local dev."
            )
            return False

        if not signature_header or not signature_header.startswith("sha256="):
            return False

        expected_sig = "sha256=" + hmac.new(
            secret.encode("utf-8"),
            payload_bytes,
            hashlib.sha256,
        ).hexdigest()

        return hmac.compare_digest(expected_sig, signature_header)

    # ------------------------------------------------------------------
    # Comment formatting
    # ------------------------------------------------------------------

    @staticmethod
    def format_review_comment(issue) -> str:
        """Format a CodeIssue as a GitHub-flavored Markdown comment body."""
        severity_emoji = {
            "critical": "🔴", "high": "🟠",
            "medium": "🟡", "low": "🔵",
        }
        emoji = severity_emoji.get(issue.severity.value, "⚪")
        lines = [
            f"{emoji} **[{issue.severity.value.upper()}] {issue.type.value.replace('_', ' ').title()}**",
            "",
            issue.message,
        ]
        if issue.suggestion:
            lines += ["", f"**Suggestion:** {issue.suggestion}"]
        if issue.fixed_snippet:
            lines += ["", "**Fixed code:**", f"```\n{issue.fixed_snippet}\n```"]
        lines += ["", "*-- CodeSentinel AI Review*"]
        return "\n".join(lines)

    @staticmethod
    def format_pr_summary(review_result) -> str:
        """Format the overall review result as a PR top-level comment."""
        score = review_result.overall_score or 0
        score_bar = "█" * (score // 10) + "░" * (10 - score // 10)

        lines = [
            "## CodeSentinel Review",
            "",
            f"**Quality Score:** `{score}/100`  `{score_bar}`",
            "",
            f"**Total Issues:** {review_result.total_issues}",
        ]

        sev = review_result.issues_by_severity
        if sev:
            lines.append("")
            lines.append("| Severity | Count |")
            lines.append("|----------|-------|")
            for s in ["critical", "high", "medium", "low"]:
                if sev.get(s, 0):
                    lines.append(f"| {s.title()} | {sev[s]} |")

        lines += ["", "---", "", review_result.overall_summary]
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_service: GitHubService | None = None


def get_github_service() -> GitHubService:
    global _service
    if _service is None:
        _service = GitHubService()
    return _service

# --- END OF FILE: github_service.py ---
