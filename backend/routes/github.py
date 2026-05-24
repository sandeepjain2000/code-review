"""
routes/github.py — GitHub webhook endpoint.

GitHub sends a POST to /webhook/github whenever a PR is opened or updated.
We verify the HMAC signature, then trigger a review asynchronously.

Setup in GitHub repo:
  Settings → Webhooks → Add webhook
  Payload URL: https://your-server.com/webhook/github
  Content type: application/json
  Secret: (same as webhook_secret in api_key.json)
  Events: Pull requests
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Request
from fastapi.responses import JSONResponse

from models.schemas import GitHubPRReviewRequest
from services.github_service import get_github_service
from services.review_service import get_review_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/webhook", tags=["webhook"])


@router.post("/github", summary="GitHub Pull Request webhook")
async def github_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    x_hub_signature_256: str = Header(None, alias="X-Hub-Signature-256"),
    x_github_event: str = Header(None, alias="X-GitHub-Event"),
):
    """
    Receive GitHub webhook events for pull requests.

    Triggered when:
    - A PR is opened
    - A PR is synchronized (new commits pushed)
    - A PR is reopened

    The review runs in the background so GitHub gets an immediate 200 response.
    """
    payload_bytes = await request.body()

    # --- Verify signature ---
    gh = get_github_service()
    if not gh.verify_webhook_signature(payload_bytes, x_hub_signature_256 or ""):
        logger.warning("Webhook signature verification failed")
        raise HTTPException(status_code=403, detail="Invalid webhook signature")

    # --- Only handle pull_request events ---
    if x_github_event != "pull_request":
        return JSONResponse({"status": "ignored", "event": x_github_event})

    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    action = payload.get("action", "")
    if action not in ("opened", "synchronize", "reopened"):
        return JSONResponse({"status": "ignored", "action": action})

    pr = payload.get("pull_request", {})
    repo = payload.get("repository", {})
    owner = repo.get("owner", {}).get("login", "")
    repo_name = repo.get("name", "")
    pull_number = pr.get("number")

    if not all([owner, repo_name, pull_number]):
        raise HTTPException(status_code=400, detail="Missing required PR fields in payload")

    # --- Fire review in background ---
    review_request = GitHubPRReviewRequest(
        owner=owner,
        repo=repo_name,
        pull_number=pull_number,
        diff_only=True,
    )

    background_tasks.add_task(_run_review_in_background, review_request)

    logger.info(
        "Webhook received: %s/%s#%d (action=%s) — review queued",
        owner, repo_name, pull_number, action,
    )

    return JSONResponse({
        "status": "queued",
        "pr": f"{owner}/{repo_name}#{pull_number}",
        "action": action,
    })


async def _run_review_in_background(request: GitHubPRReviewRequest) -> None:
    """Background task: run the review and post results to GitHub."""
    try:
        svc = get_review_service()
        result = await svc.review_github_pr(request)
        logger.info(
            "Background review complete: %s/%s#%d — %d issues found",
            request.owner, request.repo, request.pull_number, result.total_issues,
        )
    except Exception as e:
        logger.error(
            "Background review failed for %s/%s#%d: %s",
            request.owner, request.repo, request.pull_number, e,
        )

# --- END OF FILE: github.py ---
