"""
routes/review.py — Core review API endpoints.

POST /review/snippet              → Review pasted/uploaded code
POST /review/pr                   → Review a GitHub PR
POST /review/folder               → Scan and review a local folder (blocking)
POST /review/folder/start         → Start a streaming folder scan, returns job_id
GET  /review/folder/stream/{id}   → SSE stream of per-file progress events
POST /review/upload               → Multi-file upload review
DELETE /review/cache              → Purge the response cache
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

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
from config import settings
from services.cache_service import get_cache
from services.jobs_service import create_job, get_latest_job, update_job, get_job_files, log_file_done
from services.llm_service import get_llm_service
from services.review_service import _build_overall_summary, get_review_service
from services.scanner_service import get_scanner_service
from utils.circuit_breaker import get_circuit_breaker
from utils.rate_limiter import all_limiter_status

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/review", tags=["review"])

# ---------------------------------------------------------------------------
# In-memory job store for streaming folder scans
# Each job_id maps to an asyncio.Queue of SSE event dicts
# ---------------------------------------------------------------------------
_folder_jobs: Dict[str, asyncio.Queue] = {}
_paused_jobs: set[str] = set()
_active_files_per_job: Dict[str, Dict[str, dict]] = {}

# ---------------------------------------------------------------------------
# Report directory layout
#   data/reports/live/   — written continuously during the scan
#   data/reports/final/  — written once when the scan completes
# ---------------------------------------------------------------------------
_LIVE_DIR  = Path(settings.data_dir) / "reports" / "live"
_FINAL_DIR = Path(settings.data_dir) / "reports" / "final"

def _audit_log(job_id: str, action: str, details: str = "") -> None:
    """Log high-level scan actions to verify pauses and resumes."""
    audit_file = Path(settings.data_dir) / "reports" / "audit_events.log"
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_line = f"[{ts}] JOB: {job_id} | ACTION: {action} | {details}\n"
    with open(audit_file, "a", encoding="utf-8") as f:
        f.write(log_line)


# ---------------------------------------------------------------------------
# Snippet review
# ---------------------------------------------------------------------------

@router.post("/snippet", response_model=ReviewResult, summary="Review a code snippet")
async def review_snippet(request: SnippetReviewRequest):
    try:
        svc = get_review_service()
        result = await svc.review_snippet(request)
        return result
    except Exception as e:
        logger.exception("Snippet review failed")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# File upload review
# ---------------------------------------------------------------------------

@router.post("/upload", response_model=ReviewResult, summary="Review uploaded files")
async def review_upload(
    files: List[UploadFile] = File(...),
    context: Optional[str] = Form(None),
):
    svc = get_review_service()
    file_reviews = []
    for upload in files:
        try:
            content = (await upload.read()).decode("utf-8", errors="replace")
            req = SnippetReviewRequest(
                code=content,
                filename=upload.filename or "unknown",
                context=context,
            )
            single = await svc.review_snippet(req)
            if single.files:
                file_reviews.extend(single.files)
        except Exception as e:
            logger.warning("Failed to review uploaded file %s: %s", upload.filename, e)

    if not file_reviews:
        raise HTTPException(status_code=422, detail="No files could be reviewed.")

    return ReviewResult(
        review_id=uuid.uuid4().hex,
        input_type=InputType.FILE_UPLOAD,
        status=ReviewStatus.COMPLETED,
        files=file_reviews,
        overall_summary=_build_overall_summary(file_reviews),
        model_used=svc._ai._model,
    )


# ---------------------------------------------------------------------------
# GitHub PR review
# ---------------------------------------------------------------------------

@router.post("/pr", response_model=ReviewResult, summary="Review a GitHub pull request")
async def review_pr(request: GitHubPRReviewRequest):
    try:
        svc = get_review_service()
        result = await svc.review_github_pr(request)
        return result
    except Exception as e:
        logger.exception("PR review failed")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Folder scan — blocking (original, kept for CLI / API use)
# ---------------------------------------------------------------------------

@router.post("/folder", response_model=ReviewResult, summary="Scan and review a local folder")
async def review_folder(request: FolderScanRequest):
    folder = Path(request.folder_path)
    if not folder.exists():
        raise HTTPException(status_code=404, detail=f"Folder not found: {request.folder_path}")
    try:
        svc = get_review_service()
        result = await svc.review_folder(request)
        return result
    except Exception as e:
        logger.exception("Folder review failed")
        raise HTTPException(status_code=500, detail=str(e))


from pydantic import BaseModel

class FolderStatsRequest(BaseModel):
    folder_path: str
    recursive: bool = True

@router.post("/folder/stats", summary="Get statistics about a local folder")
async def folder_stats(request: FolderStatsRequest):
    folder = Path(request.folder_path)
    if not folder.exists():
        raise HTTPException(status_code=404, detail=f"Folder not found: {request.folder_path}")
    try:
        scanner = get_scanner_service()
        stats = scanner.scan_summary(request.folder_path, request.recursive)
        return stats
    except Exception as e:
        logger.exception("Folder stats retrieval failed")
        raise HTTPException(status_code=500, detail=str(e))


# ---------------------------------------------------------------------------
# Folder scan — streaming (SSE)
# ---------------------------------------------------------------------------

@router.post("/folder/start", summary="Start a streaming folder scan")
async def start_folder_scan(
    request: FolderScanRequest,
    background_tasks: BackgroundTasks,
):
    """
    Kick off a folder scan and return a job_id immediately.
    Connect to GET /review/folder/stream/{job_id} to receive live SSE events.
    """
    folder = Path(request.folder_path)
    if not folder.exists():
        raise HTTPException(status_code=404, detail=f"Folder not found: {request.folder_path}")

    job_id = uuid.uuid4().hex[:16]
    queue: asyncio.Queue = asyncio.Queue()
    _folder_jobs[job_id] = queue
    _active_files_per_job[job_id] = {}

    await create_job(
        job_id,
        str(request.folder_path),
        request.max_files,
        request.sleep_between_files,
        request.context
    )
    _audit_log(job_id, "SCAN_STARTED", f"Path: {request.folder_path}, MaxFiles: {request.max_files}, Sleep: {request.sleep_between_files}s")
    background_tasks.add_task(_run_streaming_folder_scan, job_id, request, queue, request.sleep_between_files)
    return {"job_id": job_id}


@router.post("/folder/pause/{job_id}", summary="Pause an active folder scan")
async def pause_folder_scan(job_id: str):
    """Signal the background task to stop at the next file."""
    _paused_jobs.add(job_id)
    await update_job(job_id, status="paused")
    _audit_log(job_id, "SCAN_PAUSED", "User requested pause")
    return {"status": "pause_requested"}


@router.post("/folder/resume/{job_id}", summary="Resume a paused folder scan")
async def resume_folder_scan(
    job_id: str,
    background_tasks: BackgroundTasks,
):
    """Restart a paused job from where it left off."""
    from services.jobs_service import get_job
    job = await get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    if job["status"] != "paused":
        raise HTTPException(status_code=400, detail=f"Job is not paused (status: {job['status']})")

    # Recover max_files safely: if it's missing or -1 (default for older jobs), fallback to total_files
    max_files = job.get("max_files")
    if max_files is None or max_files <= 0:
        max_files = job.get("total_files", 50)
        
    request = FolderScanRequest(
        folder_path=job["folder_path"],
        max_files=max_files,
        sleep_between_files=job.get("sleep_between_files", 5),
        context=job.get("context")
    )
    
    queue: asyncio.Queue = asyncio.Queue()
    _folder_jobs[job_id] = queue
    _active_files_per_job.setdefault(job_id, {})
    _paused_jobs.discard(job_id)  # ensure it's not in the pause set

    await update_job(job_id, status="running")
    # Pass offset positionally to match _run_streaming_folder_scan(job_id, request, queue, sleep_secs, offset)
    _audit_log(job_id, "SCAN_RESUMED", f"Path: {job['folder_path']}, Offset: {job['files_done']} files done")
    background_tasks.add_task(
        _run_streaming_folder_scan,
        job_id,
        request,
        queue,
        request.sleep_between_files,
        job["files_done"],   # offset — resume from last completed file
    )
    return {"job_id": job_id}


@router.get("/folder/stream/{job_id}", summary="SSE stream of folder scan progress")
async def stream_folder_progress(job_id: str):
    """
    SSE endpoint. Emits newline-delimited JSON events:
      {"event": "start",      "total": N, "job_id": "..."}
      {"event": "file_start", "file": "path/to/file.py", "index": 1, "total": N}
      {"event": "file_done",  "file": "path/to/file.py", "index": 1, "total": N,
       "score": 85, "issues": 3, "summary": "..."}
      {"event": "complete",   "result": { ...full ReviewResult dict... }}
      {"event": "error",      "detail": "..."}
    """
    queue = _folder_jobs.get(job_id)
    if queue is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    async def event_generator():
        # 1. Sync completed files from SQLite
        completed_files = await get_job_files(job_id)
        if completed_files:
            # Reconstruct frontend expected format
            sync_data = []
            for f in completed_files:
                sync_data.append({
                    "filename": f["filename"],
                    "issues": f["issues"],
                    "score": f["score"],
                    "error": f["error"],
                    "delayed": bool(f["delayed"]),
                    "batch": f["batch_num"],
                    "round": f["round_num"],
                    "index": f["index_num"],
                    "time_taken": f["time_taken"],
                    "size_bytes": f["size_bytes"],
                    "timestamp": f["timestamp"],
                    "tsMs": f["ts_ms"]
                })
            yield f"data: {json.dumps({'event': 'sync_completed', 'files': sync_data})}\n\n"

        # 2. Sync currently active (in-progress) files
        active = list(_active_files_per_job.get(job_id, {}).values())
        if active:
            yield f"data: {json.dumps({'event': 'sync_active', 'activeFiles': active})}\n\n"
            
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                    yield f"data: {json.dumps(item)}\n\n"
                    if item.get("event") in ("complete", "error"):
                        break
                except asyncio.TimeoutError:
                    # Send heartbeat so browser doesn't drop the connection during slow AI calls
                    yield f"data: {json.dumps({'event': 'ping'})}\n\n"
        finally:
            _folder_jobs.pop(job_id, None)
            if job_id in _active_files_per_job and not _folder_jobs:
                _active_files_per_job.pop(job_id, None)

    return StreamingResponse(event_generator(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Background task — runs the actual scan and pushes events onto the queue
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Live report helpers  (data/reports/live/)
# ---------------------------------------------------------------------------

def _live_txt_path(job_id: str) -> Path:
    _LIVE_DIR.mkdir(parents=True, exist_ok=True)
    return _LIVE_DIR / f"codesentinel_{job_id[:8]}_live.txt"


def _live_json_path(job_id: str) -> Path:
    _LIVE_DIR.mkdir(parents=True, exist_ok=True)
    return _LIVE_DIR / f"codesentinel_{job_id[:8]}_live.json"


def _init_live_report(job_id: str, folder_path: str, total: int) -> None:
    """Write the header of the live .txt report at scan start."""
    from datetime import datetime
    p = _live_txt_path(job_id)
    header = (
        "=" * 65 + chr(10) +
        "  CODESENTINEL -- LIVE SCAN REPORT (auto-saved per file)" + chr(10) +
        "=" * 65 + chr(10) +
        f"  Started   : {datetime.now().strftime('%d/%m/%Y, %H:%M:%S')}" + chr(10) +
        f"  Folder    : {folder_path}" + chr(10) +
        f"  Total     : {total} files" + chr(10) +
        f"  Report ID : {job_id[:8]}" + chr(10) +
        "  NOTE: This file updates after each file completes." + chr(10) +
        "        If the scan stops, this contains results so far." + chr(10) +
        "=" * 65 + chr(10) + chr(10)
    )
    p.write_text(header, encoding="utf-8")


def _append_file_to_live_txt(job_id: str, review, idx: int, total: int) -> None:
    """Append one file's results to the live .txt report."""
    p = _live_txt_path(job_id)
    lines = [
        "=" * 65,
        f"  FILE {idx}/{total}: {review.filename}",
        f"  Score: {review.score}/100   Issues: {len(review.issues)}   Provider: {getattr(review, 'provider', 'unknown')}   Time: {getattr(review, 'time_taken', 0.0):.2f}s",
        "",
        f"  {review.summary}" if review.summary else "",
        "",
    ]
    for issue in review.issues:
        loc = f"Line {issue.line}" if issue.line else "File-level"
        sev = issue.severity.value if hasattr(issue.severity, "value") else str(issue.severity)
        itype = issue.type.value if hasattr(issue.type, "value") else str(issue.type)
        lines.append(f"  [{sev.upper()}] {itype} -- {loc}")
        lines.append(f"  {issue.message}")
        if issue.suggestion:
            lines.append(f"  Fix: {issue.suggestion}")
        lines.append("")
        
    if getattr(review, "test_cases", None):
        lines.append("  🧪 Test Coverage Generator")
        for tc in review.test_cases:
            lines.append(f"    - {tc}")
        lines.append("")
        
    block = chr(10).join(lines) + chr(10)
    with open(p, "a", encoding="utf-8") as f:
        f.write(block)


def _update_live_json(
    job_id: str,
    file_reviews: list,
    total: int,
    folder_path: str,
    model: str,
) -> None:
    """
    Rewrite the live JSON snapshot after each file completes.

    The file reflects the scan's current state — partial results are
    preserved even if the scan is interrupted later.
    """
    from datetime import datetime

    snapshot = {
        "job_id": job_id,
        "status": "in_progress",
        "folder_path": folder_path,
        "model_used": model,
        "updated_at": datetime.now().isoformat(),
        "files_done": len(file_reviews),
        "total_files": total,
        "files": [],
    }

    total_issues = 0
    score_sum = 0
    scored_count = 0

    for fr in file_reviews:
        issues_list = []
        for iss in fr.issues:
            issues_list.append({
                "type":     iss.type.value if hasattr(iss.type, "value") else str(iss.type),
                "severity": iss.severity.value if hasattr(iss.severity, "value") else str(iss.severity),
                "message":  iss.message,
                "line":     iss.line,
                "suggestion": iss.suggestion,
                "code_snippet": iss.code_snippet,
                "fixed_snippet": getattr(iss, "fixed_snippet", None),
            })
        total_issues += len(issues_list)
        if fr.score is not None:
            score_sum += fr.score
            scored_count += 1

        snapshot["files"].append({
            "filename": fr.filename,
            "score":    fr.score,
            "summary":  fr.summary,
            "issues":   issues_list,
            "test_cases": getattr(fr, "test_cases", []),
            "time_taken": getattr(fr, "time_taken", 0.0),
            "provider": getattr(fr, "provider", "")
        })

    snapshot["total_issues"] = total_issues
    snapshot["overall_score"] = round(score_sum / scored_count) if scored_count else None

    p = _live_json_path(job_id)
    try:
        p.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write live JSON report: %s", exc)


# ---------------------------------------------------------------------------
# Final report helpers  (data/reports/final/)
# ---------------------------------------------------------------------------

def _build_text_report(result) -> str:
    """Build a human-readable plain-text report."""
    from datetime import datetime
    ts = datetime.now().strftime("%d/%m/%Y, %H:%M:%S")
    sev = result.issues_by_severity or {}
    lines = [
        "=" * 65,
        "  CODESENTINEL -- AI CODE REVIEW REPORT",
        "=" * 65,
        f"  Generated : {ts}",
        f"  Review ID : {result.review_id}",
        f"  Model     : {result.model_used}",
        f"  Score     : {result.overall_score if result.overall_score is not None else 'N/A'} / 100",
        f"  Files     : {len(result.files)}",
        f"  Issues    : {result.total_issues}",
        "",
    ]
    for sname in ("critical", "high", "medium", "low"):
        cnt = sev.get(sname, 0)
        if cnt:
            lines.append(f"  {sname.upper():<10} {cnt}")
    lines += [
        "",
        "-" * 65,
        "  SUMMARY",
        "-" * 65,
        result.overall_summary or "",
        "",
    ]
    for f in result.files:
        lines += [
            "=" * 65,
            f"  FILE: {f.filename}",
            f"  Score: {f.score}/100   Issues: {len(f.issues)}   Provider: {getattr(f, 'provider', 'unknown')}   Time: {getattr(f, 'time_taken', 0.0):.2f}s",
            "",
            f"  {f.summary}" if f.summary else "",
            "",
        ]
        for issue in f.issues:
            loc = f"Line {issue.line}" if issue.line else "File-level"
            lines.append(f"  [{issue.severity.upper()}] {issue.type} -- {loc}")
            lines.append(f"  {issue.message}")
            if issue.suggestion:
                lines.append(f"  Fix: {issue.suggestion}")
            if issue.code_snippet:
                lines.append(f"  Code: {issue.code_snippet[:120]}")
            lines.append("")
            
        if getattr(f, "test_cases", None):
            lines.append("  🧪 Test Coverage Generator")
            for tc in f.test_cases:
                lines.append(f"    - {tc}")
            lines.append("")
            
    lines += ["=" * 65, "  END OF REPORT", "=" * 65]
    return chr(10).join(lines)


def _save_html_report(result) -> Path:
    """Read frontend/index.html and inject JSON result into it."""
    _FINAL_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"codesentinel_{result.review_id[:8]}"
    html_path = _FINAL_DIR / f"{stem}.html"
    
    frontend_path = Path(__file__).resolve().parent.parent.parent / "frontend" / "index.html"
    try:
        if frontend_path.is_file():
            html_content = frontend_path.read_text(encoding="utf-8")
            
            # Capture health snapshot
            breaker_status = get_circuit_breaker().status()
            limiter_status = all_limiter_status()
            all_providers = sorted(set(list(breaker_status.keys()) + list(limiter_status.keys())))
            health_snapshot = {}
            for p in all_providers:
                health_snapshot[p] = {
                    "circuit": breaker_status.get(p, {}),
                    "rate_limiter": limiter_status.get(p, {}),
                }
            
            data = result.model_dump()
            data["provider_health"] = health_snapshot
            
            json_data = json.dumps(data, default=str).replace("<", "\\u003c").replace("</script>", "<\\/script>")
            script_tag = f"<script>window.__INITIAL_DATA__ = {json_data};</script>"
            html_content = html_content.replace("<!-- REPORT_DATA_INJECT -->", script_tag)
            html_path.write_text(html_content, encoding="utf-8")
            logger.info("Final HTML report saved with health snapshot: %s", html_path)
    except Exception as e:
        logger.error("Failed to generate HTML report: %s", e)
    
    return html_path

def _save_report_to_disk(result) -> Path:
    """Save the completed review as both .txt, .json and .html to data/reports/final/."""
    _FINAL_DIR.mkdir(parents=True, exist_ok=True)
    stem = f"codesentinel_{result.review_id[:8]}"
    txt_path  = _FINAL_DIR / f"{stem}.txt"
    json_path = _FINAL_DIR / f"{stem}.json"
    txt_path.write_text(_build_text_report(result), encoding="utf-8")
    json_path.write_text(json.dumps(result.model_dump(), indent=2, default=str), encoding="utf-8")
    _save_html_report(result)
    logger.info("Final report saved: %s", txt_path)
    return txt_path


# ---------------------------------------------------------------------------
# Batch report helpers  (data/reports/live/  per-batch)
# ---------------------------------------------------------------------------

def _batch_txt_path(job_id: str, batch_num: int) -> Path:
    _LIVE_DIR.mkdir(parents=True, exist_ok=True)
    return _LIVE_DIR / f"codesentinel_{job_id[:8]}_batch{batch_num}_live.txt"


def _batch_json_path(job_id: str, batch_num: int) -> Path:
    _LIVE_DIR.mkdir(parents=True, exist_ok=True)
    return _LIVE_DIR / f"codesentinel_{job_id[:8]}_batch{batch_num}_live.json"


def _save_batch_report(
    job_id: str,
    batch_num: int,
    batch_reviews: list,
    total_files: int,
    folder_path: str,
    model: str,
    batch_start_idx: int,
) -> Path:
    """Save a completed batch's results as both .txt and .json."""
    from datetime import datetime
    p_txt  = _batch_txt_path(job_id, batch_num)
    p_json = _batch_json_path(job_id, batch_num)

    # --- TXT ---
    lines = [
        "=" * 65,
        f"  CODESENTINEL -- BATCH {batch_num} REPORT",
        f"  Files {batch_start_idx} - {batch_start_idx + len(batch_reviews) - 1} of {total_files}",
        f"  Folder : {folder_path}",
        f"  Saved  : {datetime.now().strftime('%d/%m/%Y, %H:%M:%S')}",
        "=" * 65, "",
    ]
    scores = [r.score for r in batch_reviews if r.score is not None]
    batch_score = round(sum(scores) / len(scores)) if scores else None
    lines.append(f"  Batch Score   : {batch_score}/100" if batch_score else "  Batch Score   : N/A")
    lines.append(f"  Files Reviewed: {len(batch_reviews)}")
    lines.append(f"  Total Issues  : {sum(len(r.issues) for r in batch_reviews)}")
    lines.append("")
    for r in batch_reviews:
        lines += ["=" * 65, f"  FILE: {r.filename}",
                  f"  Score: {r.score}/100   Issues: {len(r.issues)}   Provider: {getattr(r, 'provider', 'unknown')}   Time: {getattr(r, 'time_taken', 0.0):.2f}s", "",
                  f"  {r.summary}" if r.summary else "", ""]
        for issue in r.issues:
            loc = f"Line {issue.line}" if issue.line else "File-level"
            sev = issue.severity.value if hasattr(issue.severity, "value") else str(issue.severity)
            itype = issue.type.value if hasattr(issue.type, "value") else str(issue.type)
            lines.append(f"  [{sev.upper()}] {itype} -- {loc}")
            lines.append(f"  {issue.message}")
            if issue.suggestion:
                lines.append(f"  Fix: {issue.suggestion}")
            lines.append("")
            
        if getattr(r, "test_cases", None):
            lines.append("  🧪 Test Coverage Generator")
            for tc in r.test_cases:
                lines.append(f"    - {tc}")
            lines.append("")
            
    p_txt.write_text("\n".join(lines), encoding="utf-8")

    # --- JSON ---
    issues_list_all = []
    for r in batch_reviews:
        for iss in r.issues:
            issues_list_all.append({
                "filename": r.filename,
                "type":     iss.type.value if hasattr(iss.type, "value") else str(iss.type),
                "severity": iss.severity.value if hasattr(iss.severity, "value") else str(iss.severity),
                "message":  iss.message,
                "line":     iss.line,
                "suggestion": iss.suggestion,
            })
    snapshot = {
        "job_id": job_id,
        "batch": batch_num,
        "folder_path": folder_path,
        "model_used": model,
        "saved_at": datetime.now().isoformat(),
        "files": [{"filename": r.filename, "score": r.score,
                   "summary": r.summary, "issues_count": len(r.issues),
                   "test_cases": getattr(r, "test_cases", []),
                   "time_taken": getattr(r, "time_taken", 0.0),
                   "provider": getattr(r, "provider", "")} for r in batch_reviews],
        "batch_score": batch_score,
        "total_issues": len(issues_list_all),
        "issues": issues_list_all,
    }
    try:
        p_json.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    except OSError as exc:
        logger.warning("Could not write batch JSON: %s", exc)
    logger.info("[%s] Batch %d report saved: %s", job_id[:8], batch_num, p_txt)
    return p_txt



# ---------------------------------------------------------------------------
# Windows sleep prevention (no-op on non-Windows)
# ---------------------------------------------------------------------------

_ES_CONTINUOUS      = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001  # keeps CPU/network alive, does NOT keep screen on

def _prevent_sleep() -> None:
    """Tell Windows not to sleep while a scan is running."""
    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(
                _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED
            )
            logger.info("Sleep prevention: ENABLED (scan in progress)")
        except Exception as e:
            logger.warning("Could not enable sleep prevention: %s", e)

def _restore_sleep() -> None:
    """Restore normal Windows sleep behaviour after scan finishes."""
    if sys.platform == "win32":
        try:
            ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
            logger.info("Sleep prevention: DISABLED (scan finished)")
        except Exception as e:
            logger.warning("Could not restore sleep settings: %s", e)


async def _run_streaming_folder_scan(
    job_id: str,
    request: FolderScanRequest,
    queue: asyncio.Queue,
    sleep_secs: int = 5,
    offset: int = 0,
) -> None:
    svc = get_review_service()
    scanner = get_scanner_service()

    # Rebuild the rate limiter so it reflects the current NVIDIA key pool
    from utils.rate_limiter import reset_rate_limiter
    reset_rate_limiter()

    _prevent_sleep()  # Keep PC awake for the duration of the scan

    file_reviews: List[FileReview] = []
    model_used = svc._ai._model
    total = 0
    status = ReviewStatus.COMPLETED
    error_detail = None

    try:
        source_files = scanner.scan(request.folder_path, request.recursive, request.max_files)
        total = len(source_files)

        # --- Determine batching ---
        import math
        raw_batch_size = settings.batch_size  # 0 = no batching
        use_batching = raw_batch_size > 0 and total > raw_batch_size
        batch_size = raw_batch_size if use_batching else total
        total_batches = math.ceil(total / batch_size) if use_batching else 1

        await queue.put({
            "event": "start",
            "total": total,
            "job_id": job_id,
            "total_batches": total_batches,
            "batch_size": batch_size if use_batching else 0,
        })
        _init_live_report(job_id, str(request.folder_path), total)
        await update_job(job_id, total_files=total, batch_size=batch_size, total_batches=total_batches)

        file_summaries: List[dict] = []
        consecutive_failures = 0
        max_failures = settings.scanner.max_consecutive_failures
        pending_tasks = {}

        # ── Shared concurrency primitives (live across ALL batches) ──────────
        _cfg_limit = settings.scanner.max_concurrent_files
        if _cfg_limit <= 0:
            _cfg_limit = max(1, len(settings.nvidia_accounts)) if settings.nvidia_accounts else 10
        sem        = asyncio.Semaphore(_cfg_limit)
        state_lock = asyncio.Lock()
        logger.info("[%s] Pipeline concurrency=%d (keys=%d)",
                    job_id[:8], _cfg_limit, len(settings.nvidia_accounts))

        # batch_reviews_map: batch_num -> list of FileReview for that batch
        batch_reviews_map: dict = {b: [] for b in range(1, total_batches + 1)}

        # ── _process_file defined ONCE; file_batch_num passed explicitly ─────
        async def _process_file(sf, sf_idx, file_batch_num):
            nonlocal consecutive_failures

            if job_id in _paused_jobs:
                return

            _should_sleep = False
            async with sem:
                if job_id in _paused_jobs:
                    return

                async with state_lock:
                    _active_files_per_job.setdefault(job_id, {})[sf.relative_path] = {
                        "filename": sf.relative_path, "index": sf_idx, "batch": file_batch_num
                    }

                logger.info("")
                await queue.put({
                    "event": "file_start",
                    "file": sf.relative_path,
                    "index": sf_idx,
                    "total": total,
                    "batch": file_batch_num,
                    "size_bytes": sf.size_bytes,
                })

                try:
                    async with state_lock:
                        current_context = list(file_summaries)

                    task = asyncio.create_task(svc._ai.review_file(
                        code=sf.content,
                        filename=sf.relative_path,
                        context=request.context,
                        focus_areas=request.focus_areas,
                        file_summaries=current_context,
                    ))

                    try:
                        review = await asyncio.wait_for(asyncio.shield(task), timeout=180.0)
                    except asyncio.TimeoutError:
                        logger.info("File %s took > 180s, pushing to Round 2", sf.relative_path)
                        async with state_lock:
                            pending_tasks[sf.relative_path] = (task, sf_idx, sf, file_batch_num)
                            _active_files_per_job.get(job_id, {}).pop(sf.relative_path, None)
                        payload = {
                            "event": "file_delayed",
                            "file": sf.relative_path,
                            "index": sf_idx,
                            "total": total,
                            "summary": "Processing took too long. Pushed to Round 2.",
                            "batch": file_batch_num,
                            "size_bytes": sf.size_bytes,
                        }
                        await queue.put(payload)
                        import time
                        from datetime import datetime
                        await log_file_done(job_id, {
                            "filename": sf.relative_path, "index": sf_idx, "batch": file_batch_num,
                            "round": 1, "score": None, "issues": 0, "error": None, "delayed": True,
                            "time_taken": 0, "size_bytes": sf.size_bytes,
                            "timestamp": datetime.now().strftime("%I:%M:%S %p"), "tsMs": time.time() * 1000
                        })
                        return

                    async with state_lock:
                        consecutive_failures = 0
                        file_reviews.append(review)
                        batch_reviews_map[file_batch_num].append(review)
                        file_summaries.append({"filename": review.filename, "summary": review.summary})
                        current_files_done = len(file_reviews)

                    logger.info("[%s] Progress: %d/%d - %s - Score: %s",
                                job_id[:8], sf_idx, total, sf.relative_path, review.score)

                    payload = {
                        "event": "file_done",
                        "file": sf.relative_path,
                        "index": sf_idx,
                        "total": total,
                        "score": review.score,
                        "issues": len(review.issues),
                        "summary": review.summary[:200] if review.summary else "",
                        "batch": file_batch_num,
                        "time_taken": review.time_taken,
                        "size_bytes": sf.size_bytes,
                    }
                    await queue.put(payload)
                    import time
                    from datetime import datetime
                    await log_file_done(job_id, {
                        "filename": sf.relative_path, "index": sf_idx, "batch": file_batch_num,
                        "round": 1, "score": review.score, "issues": len(review.issues), "error": None, "delayed": False,
                        "time_taken": review.time_taken, "size_bytes": sf.size_bytes,
                        "timestamp": datetime.now().strftime("%I:%M:%S %p"), "tsMs": time.time() * 1000
                    })

                    async with state_lock:
                        _append_file_to_live_txt(job_id, review, sf_idx, total)
                        _update_live_json(job_id, file_reviews, total, str(request.folder_path), model_used)
                        _active_files_per_job.get(job_id, {}).pop(sf.relative_path, None)

                    await update_job(job_id, files_done=current_files_done)
                    _should_sleep = sf_idx < total

                except Exception as e:
                    async with state_lock:
                        consecutive_failures += 1
                        current_failures = consecutive_failures
                        failed = FileReview(filename=sf.relative_path, summary=f"Review failed: {e}")
                        file_reviews.append(failed)
                        batch_reviews_map[file_batch_num].append(failed)
                        current_files_done = len(file_reviews)

                    logger.error("[%s] Progress: %d/%d - FAILED: %s -- %s",
                                 job_id[:8], sf_idx, total, sf.relative_path, e)

                    payload = {
                        "event": "file_done",
                        "file": sf.relative_path,
                        "index": sf_idx,
                        "total": total,
                        "score": None,
                        "issues": 0,
                        "summary": f"Error: {e}",
                        "batch": file_batch_num,
                        "time_taken": 0.0,
                        "size_bytes": sf.size_bytes,
                    }
                    await queue.put(payload)
                    import time
                    from datetime import datetime
                    await log_file_done(job_id, {
                        "filename": sf.relative_path, "index": sf_idx, "batch": file_batch_num,
                        "round": 1, "score": None, "issues": 0, "error": f"Error: {e}", "delayed": False,
                        "time_taken": 0.0, "size_bytes": sf.size_bytes,
                        "timestamp": datetime.now().strftime("%I:%M:%S %p"), "tsMs": time.time() * 1000
                    })

                    async with state_lock:
                        _append_file_to_live_txt(job_id, failed, sf_idx, total)
                        _update_live_json(job_id, file_reviews, total, str(request.folder_path), model_used)
                        _active_files_per_job.get(job_id, {}).pop(sf.relative_path, None)

                    await update_job(job_id, files_done=current_files_done)

                    if current_failures >= max_failures:
                        reason = (
                            f"Scan aborted: {current_failures} consecutive file failures. "
                            f"Last error: {e}"
                        )
                        logger.error(reason)
                        await queue.put({"event": "error", "detail": reason})
                        raise RuntimeError(reason)
                    _should_sleep = sf_idx < total

            # Semaphore released — sleep for rate-limit courtesy
            if _should_sleep and sleep_secs > 0:
                await asyncio.sleep(sleep_secs)

        # ── Pre-launch ALL file tasks upfront (pipeline mode) ─────────────────
        # The semaphore caps concurrency. As files from batch N complete, their
        # slots are immediately filled by files from batch N+1 — no idle gaps.
        all_batch_tasks: dict = {}  # batch_num -> [asyncio.Task]
        global_idx = 0
        for b in range(1, total_batches + 1):
            b_start = (b - 1) * batch_size
            b_end   = min(b * batch_size, total)
            batch_task_list = []
            for sf in source_files[b_start:b_end]:
                global_idx += 1
                if global_idx <= offset:
                    continue
                t = asyncio.create_task(_process_file(sf, global_idx, b))
                batch_task_list.append(t)
            all_batch_tasks[b] = batch_task_list

        # ── Batch loop: send events + save reports (tasks are already running) ─
        global_idx = offset
        for batch_num in range(1, total_batches + 1):
            batch_start = (batch_num - 1) * batch_size
            batch_end   = min(batch_num * batch_size, total)

            if use_batching:
                logger.info("")
                logger.info("[%s] === BATCH %d/%d === (files %d-%d of %d)",
                            job_id[:8], batch_num, total_batches, batch_start+1, batch_end, total)
                await queue.put({
                    "event": "batch_start",
                    "batch": batch_num,
                    "total_batches": total_batches,
                    "files_in_batch": batch_end - batch_start,
                    "batch_start_idx": batch_start + 1,
                })
                await update_job(job_id, current_batch=batch_num)
                global_idx = batch_end  # keep in sync

            tasks = all_batch_tasks.get(batch_num, [])
            if tasks:
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for res in results:
                    if isinstance(res, Exception) and not isinstance(res, RuntimeError):
                        logger.error("Unhandled exception in file task: %s", res)
                    elif isinstance(res, RuntimeError):
                        raise res

            if job_id in _paused_jobs:
                _paused_jobs.discard(job_id)
                logger.info("Job %s paused by user at batch %d", job_id, batch_num)
                await queue.put({"event": "paused", "index": batch_end, "total": total})
                return

            # ---- Save batch report ----
            batch_reviews = batch_reviews_map.get(batch_num, [])
            if use_batching and batch_reviews:
                done_reviews = [r for r in batch_reviews if r.score is not None]
                batch_score  = round(sum(r.score for r in done_reviews) / len(done_reviews)) if done_reviews else None
                batch_issues = sum(len(r.issues) for r in batch_reviews)
                _save_batch_report(
                    job_id, batch_num, batch_reviews, total,
                    str(request.folder_path), model_used, batch_start + 1
                )
                batch_txt_fn = f"codesentinel_{job_id[:8]}_batch{batch_num}_live.txt"
                logger.info("[%s] === BATCH %d/%d DONE === Score: %s, Issues: %d",
                            job_id[:8], batch_num, total_batches, batch_score, batch_issues)
                await queue.put({
                    "event": "batch_done",
                    "batch": batch_num,
                    "total_batches": total_batches,
                    "files": len(batch_reviews),
                    "score": batch_score,
                    "issues": batch_issues,
                    "report_file": batch_txt_fn,
                    "batch_start_idx": batch_start + 1,
                    "batch_end_idx": batch_end,
                })


        # ---- ROUND 2: cancel stalled tasks, re-submit through semaphore ------
        if pending_tasks:
            await queue.put({"event": "round2_start", "total": len(pending_tasks)})
            logger.info("")
            logger.info("Round 2: cancelling %d stalled tasks and re-submitting through semaphore",
                        len(pending_tasks))

            async def _process_file_r2(stalled_task, idx, sf, orig_batch):
                """Cancel the stalled HTTP connection, get a fresh one through sem."""
                stalled_task.cancel()
                try:
                    await asyncio.wait_for(stalled_task, timeout=1.0)
                except Exception:
                    pass

                async with sem:
                    async with state_lock:
                        _active_files_per_job.setdefault(job_id, {})[sf.relative_path] = {
                            "filename": sf.relative_path, "index": idx, "batch": orig_batch
                        }

                    await queue.put({
                        "event": "file_start",
                        "file": sf.relative_path,
                        "index": idx,
                        "total": total,
                        "batch": orig_batch,
                        "size_bytes": sf.size_bytes,
                        "round2": True,
                    })

                    try:
                        async with state_lock:
                            # Limit context to avoid inflating token count for large files
                            ctx_snapshot = list(file_summaries[:80])

                        review = await asyncio.wait_for(
                            svc._ai.review_file(
                                code=sf.content,
                                filename=sf.relative_path,
                                context=request.context,
                                focus_areas=request.focus_areas,
                                file_summaries=ctx_snapshot,
                            ),
                            timeout=180.0
                        )
                        review.is_round2 = True

                        async with state_lock:
                            file_reviews.append(review)
                            file_summaries.append({"filename": review.filename,
                                                   "summary": review.summary})
                            _active_files_per_job.get(job_id, {}).pop(sf.relative_path, None)

                        logger.info("[%s] R2 done: %d/%d - %s - Score: %s",
                                    job_id[:8], idx, total, sf.relative_path, review.score)
                        await queue.put({
                            "event": "file_done",
                            "file": sf.relative_path,
                            "index": idx,
                            "total": total,
                            "score": review.score,
                            "issues": len(review.issues),
                            "summary": review.summary[:200] if review.summary else "",
                            "time_taken": review.time_taken,
                            "size_bytes": sf.size_bytes,
                            "batch": orig_batch,
                            "round2": True,
                        })
                        _append_file_to_live_txt(job_id, review, idx, total)
                        async with state_lock:
                            _update_live_json(job_id, file_reviews, total,
                                              str(request.folder_path), model_used)
                        await update_job(job_id, files_done=len(file_reviews))

                        if sleep_secs > 0:
                            await asyncio.sleep(sleep_secs)

                    except Exception as e:
                        logger.error("[%s] R2 failed: %s - %s", job_id[:8], sf.relative_path, e)
                        failed = FileReview(filename=sf.relative_path,
                                            summary=f"Round 2 failed: {e}", is_round2=True)
                        async with state_lock:
                            file_reviews.append(failed)
                            _active_files_per_job.get(job_id, {}).pop(sf.relative_path, None)
                        await queue.put({
                            "event": "file_done",
                            "file": sf.relative_path,
                            "index": idx,
                            "total": total,
                            "score": None,
                            "issues": 0,
                            "summary": f"Error: {e}",
                            "time_taken": 0.0,
                            "size_bytes": sf.size_bytes,
                            "batch": orig_batch,
                            "round2": True,
                        })
                        _append_file_to_live_txt(job_id, failed, idx, total)
                        async with state_lock:
                            _update_live_json(job_id, file_reviews, total,
                                              str(request.folder_path), model_used)
                        await update_job(job_id, files_done=len(file_reviews))

            r2_tasks = [
                asyncio.create_task(_process_file_r2(task, idx, sf, orig_batch))
                for _path, (task, idx, sf, orig_batch) in pending_tasks.items()
            ]
            results = await asyncio.gather(*r2_tasks, return_exceptions=True)
            for res in results:
                if isinstance(res, Exception):
                    logger.error("Unhandled R2 task exception: %s", res)

    except Exception as e:
        logger.exception("Streaming folder scan failed for job %s", job_id)
        status = ReviewStatus.FAILED
        error_detail = str(e)
        if not isinstance(e, RuntimeError):
            await queue.put({"event": "error", "detail": str(e)})

    # ALWAYS SAVE ON EXIT (Partial or Complete)
    try:
        import time as _time
        successful_files = len([f for f in file_reviews if f.score is not None])
        coverage = int((successful_files / max(1, total)) * 100)
        confidence = "HIGH" if coverage == 100 else ("MEDIUM" if coverage >= 50 else "LOW")

        result = ReviewResult(
            review_id=job_id,
            input_type=InputType.FOLDER_SCAN,
            status=status,
            files=file_reviews,
            overall_summary=_build_overall_summary(
                file_reviews, folder=str(request.folder_path), coverage=coverage
            ) if file_reviews else "No files reviewed.",
            model_used=model_used,
            error=error_detail,
            coverage=coverage,
            confidence=confidence
        )
        txt_path = _save_report_to_disk(result)

        if status == ReviewStatus.COMPLETED:
            await queue.put({"event": "complete", "result": result.model_dump()})

        await update_job(
            job_id,
            status="complete" if status == ReviewStatus.COMPLETED else "error",
            files_done=len(file_reviews),
            end_time=_time.time(),
            overall_score=result.overall_score,
            total_issues=result.total_issues,
            model_used=result.model_used,
            report_path=str(txt_path),
            error_detail=error_detail
        )
    except Exception as save_err:
        logger.error("Failed to save partial report on exit: %s", save_err)
    finally:
        _restore_sleep()  # Always re-enable sleep when scan exits


@router.get("/folder/jobs/latest", summary="Get the latest scan job status")
async def get_latest_scan_job():
    """Returns the most recent scan job so the frontend can restore state."""
    job = await get_latest_job()
    return {"job": job}


# ---------------------------------------------------------------------------
# Report download
# ---------------------------------------------------------------------------

@router.get("/reports/{filename}", summary="Download a saved report file")
async def download_report(filename: str):
    """Serve a saved .txt or .json report from data/reports/ (live or final)."""
    from fastapi.responses import FileResponse
    safe = Path(filename).name  # strip any path traversal
    # Search final first, then live
    for directory in (_FINAL_DIR, _LIVE_DIR):
        p = directory / safe
        if p.exists():
            media = "application/json" if safe.endswith(".json") else "text/plain"
            return FileResponse(str(p), media_type=media, filename=safe)
    raise HTTPException(status_code=404, detail=f"Report not found: {filename}")


# ---------------------------------------------------------------------------
# Cache management
# ---------------------------------------------------------------------------

@router.delete("/cache", summary="Clear the review cache")
async def clear_cache():
    cache = get_cache()
    await cache.clear()
    return {"status": "cache cleared"}

# --- END OF FILE: review.py ---
