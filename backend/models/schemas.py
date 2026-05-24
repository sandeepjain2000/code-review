"""
schemas.py — All Pydantic request/response models for the Code Review API.
"""

from __future__ import annotations

from enum import Enum
from typing import Dict, List, Optional
from pydantic import BaseModel, Field, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class Severity(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class IssueType(str, Enum):
    BUG = "bug"
    SECURITY = "security"
    PERFORMANCE = "performance"
    READABILITY = "readability"
    MAINTAINABILITY = "maintainability"
    BEST_PRACTICE = "best_practice"
    TYPE_ERROR = "type_error"
    DEAD_CODE = "dead_code"
    DEPENDENCY = "dependency"
    # Extended types returned by the enhanced prompt
    ERROR_HANDLING = "error_handling"
    OBSERVABILITY = "observability"
    DATA_INTEGRITY = "data_integrity"
    RELIABILITY = "reliability"
    MULTI_TENANCY = "multi_tenancy"
    GDPR = "gdpr"
    CONFIGURATION = "configuration"
    SCALABILITY = "scalability"
    RUNTIME = "runtime"
    DATABASE = "database"
    CONCURRENCY = "concurrency"
    MEMORY = "memory"
    NETWORK = "network"
    LOGIC = "logic"
    VALIDATION = "validation"
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    IDEMPOTENCY = "idempotency"
    EDGE_CASE = "edge_case"
    BUSINESS_LOGIC = "business_logic"
    SECURITY_ABUSE = "security_abuse"
    ENVIRONMENT_DRIFT = "environment_drift"
    COGNITIVE_COMPLEXITY = "cognitive_complexity"
    DATA_LIFECYCLE = "data_lifecycle"
    UIUX = "uiux"
    SAAS_COMPLETENESS = "saas_completeness"
    UX_COPY = "ux_copy"
    # Common AI aliases — kept here to prevent valid issues being dropped
    CODE_SMELL = "code_smell"
    NULL_POINTER_EXCEPTION = "null_pointer_exception"
    NULL_CHECK = "null_check"
    INPUT_VALIDATION = "input_validation"
    SQL_INJECTION = "sql_injection"
    CODE_QUALITY = "code_quality"
    # §31–32: Stack, role, chunk, and advanced hardening types
    CSV_INJECTION = "csv_injection"
    CLIENT_SECRET = "client_secret"
    UPLOAD_ABUSE = "upload_abuse"
    AUTH_HARDENING = "auth_hardening"
    TLS_SSL = "tls_ssl"
    WEBHOOK_SECURITY = "webhook_security"
    FEATURE_FLAG = "feature_flag"
    NEXTJS_AUTH = "nextjs_auth"
    CHUNK_PARTIAL = "chunk_partial"



class ReviewStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class InputType(str, Enum):
    SNIPPET = "snippet"
    FILE_UPLOAD = "file_upload"
    GITHUB_PR = "github_pr"
    FOLDER_SCAN = "folder_scan"


# ---------------------------------------------------------------------------
# Core data models
# ---------------------------------------------------------------------------

class CodeIssue(BaseModel):
    """A single code issue identified during review."""
    line: Optional[int] = Field(None, description="Line number where issue occurs (null = file-level)")
    line_end: Optional[int] = Field(None, description="End line for multi-line issues")
    severity: Severity
    type: IssueType
    message: str = Field(..., description="Clear description of the problem")
    suggestion: str = Field(..., description="Concrete fix or improvement")
    code_snippet: Optional[str] = Field(None, description="Problematic code excerpt")
    fixed_snippet: Optional[str] = Field(None, description="Suggested corrected code")


class FileReview(BaseModel):
    """Review results for a single file."""
    filename: str
    language: Optional[str] = None
    total_lines: Optional[int] = None
    issues: List[CodeIssue] = []
    test_cases: List[str] = Field([], description="List of generated test scenarios covering happy path, edge cases, failure states, and concurrency.")
    summary: str = ""
    score: Optional[int] = Field(None, ge=0, le=100, description="Overall code quality score 0-100")
    was_chunked: bool = False
    chunks_processed: int = 1
    provider: str = ""
    time_taken: float = 0.0
    is_round2: bool = False


class ReviewResult(BaseModel):
    """Complete review result (one or many files)."""
    review_id: str
    input_type: InputType
    status: ReviewStatus
    files: List[FileReview] = []
    overall_summary: str = ""
    total_issues: int = 0
    issues_by_severity: Dict[str, int] = {}
    issues_by_type: Dict[str, int] = {}
    overall_score: Optional[int] = Field(None, ge=0, le=100)
    model_used: str = ""
    tokens_used: int = 0
    cached: bool = False
    error: Optional[str] = None
    coverage: Optional[int] = Field(100, description="Percentage of files successfully analyzed")
    confidence: Optional[str] = Field("HIGH", description="Confidence in the overall score (HIGH/MEDIUM/LOW)")

    @model_validator(mode="after")
    def compute_aggregates(self) -> "ReviewResult":
        all_issues = [issue for f in self.files for issue in f.issues]
        self.total_issues = len(all_issues)

        sev_counts: Dict[str, int] = {}
        type_counts: Dict[str, int] = {}
        for issue in all_issues:
            sev_counts[issue.severity.value] = sev_counts.get(issue.severity.value, 0) + 1
            type_counts[issue.type.value] = type_counts.get(issue.type.value, 0) + 1

        self.issues_by_severity = sev_counts
        self.issues_by_type = type_counts

        scored = [f.score for f in self.files if f.score is not None]
        if scored:
            self.overall_score = round(sum(scored) / len(scored))

        return self


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class SnippetReviewRequest(BaseModel):
    """Review a code snippet pasted directly."""
    code: str = Field(..., min_length=1, description="Source code to review")
    filename: str = Field("snippet.py", description="Filename hint for language detection")
    language: Optional[str] = Field(None, description="Language override (python, javascript, etc.)")
    context: Optional[str] = Field(None, description="Additional context about what the code does")
    focus_areas: Optional[List[IssueType]] = Field(None, description="Limit review to specific issue types")


class FileUploadReviewRequest(BaseModel):
    """Review one or more uploaded files (used after multipart upload)."""
    filenames: List[str]
    context: Optional[str] = None
    focus_areas: Optional[List[IssueType]] = None


class GitHubPRReviewRequest(BaseModel):
    """Review a GitHub pull request."""
    owner: str = Field(..., description="GitHub repo owner (user or org)")
    repo: str = Field(..., description="Repository name")
    pull_number: int = Field(..., description="Pull request number")
    diff_only: bool = Field(True, description="Only review changed lines (recommended)")
    context: Optional[str] = None
    focus_areas: Optional[List[IssueType]] = None


class FolderScanRequest(BaseModel):
    """Review all code files in a folder tree."""
    folder_path: str = Field(..., description="Absolute path to the folder to scan")
    recursive: bool = True
    max_files: int = Field(50, ge=1, le=9999)
    sleep_between_files: int = Field(30, ge=0, le=120, description="Seconds to sleep between file reviews (reduces rate limit pressure)")
    context: Optional[str] = None
    focus_areas: Optional[List[IssueType]] = None


# ---------------------------------------------------------------------------
# GitHub Webhook models
# ---------------------------------------------------------------------------

class GitHubWebhookPayload(BaseModel):
    """Minimal webhook payload we care about."""
    action: Optional[str] = None
    number: Optional[int] = None
    pull_request: Optional[Dict] = None
    repository: Optional[Dict] = None
    sender: Optional[Dict] = None


# ---------------------------------------------------------------------------
# Job models (async queue)
# ---------------------------------------------------------------------------

class ReviewJob(BaseModel):
    """Async job tracking."""
    job_id: str
    status: ReviewStatus
    created_at: str
    completed_at: Optional[str] = None
    result_id: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

class HealthResponse(BaseModel):
    status: str = "ok"
    version: str = "1.0.0"
    model: str = ""
    cache_enabled: bool = False

# --- END OF FILE: schemas.py ---
