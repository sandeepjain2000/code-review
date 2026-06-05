"""
review_categories.py — Review focus categories for API and UI.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from models.schemas import IssueType


# Grouped categories shown in the UI (each id maps to IssueType.value)
REVIEW_CATEGORY_GROUPS: List[Dict[str, Any]] = [
    {
        "id": "production",
        "label": "Production & operations",
        "categories": [
            {"id": "production_readiness", "label": "Production readiness"},
            {"id": "reliability", "label": "Reliability"},
            {"id": "error_handling", "label": "Error handling"},
            {"id": "observability", "label": "Observability"},
            {"id": "configuration", "label": "Configuration"},
            {"id": "release_readiness", "label": "Release readiness"},
            {"id": "rollback_confidence", "label": "Rollback confidence"},
            {"id": "change_risk", "label": "Change risk"},
            {"id": "slo_impact", "label": "SLO impact"},
            {"id": "ownership_runbook", "label": "Ownership & runbooks"},
        ],
    },
    {
        "id": "security",
        "label": "Security & compliance",
        "categories": [
            {"id": "security", "label": "Security"},
            {"id": "authentication", "label": "Authentication"},
            {"id": "authorization", "label": "Authorization"},
            {"id": "multi_tenancy", "label": "Multi-tenancy"},
            {"id": "gdpr", "label": "GDPR / data protection"},
            {"id": "compliance", "label": "Compliance (PCI/HIPAA/SOC2)"},
            {"id": "security_abuse", "label": "Abuse cases"},
            {"id": "sql_injection", "label": "SQL injection"},
            {"id": "auth_hardening", "label": "Auth hardening"},
            {"id": "tls_ssl", "label": "TLS / SSL"},
            {"id": "webhook_security", "label": "Webhooks"},
            {"id": "client_secret", "label": "Client secrets"},
            {"id": "upload_abuse", "label": "Upload abuse"},
            {"id": "csv_injection", "label": "CSV injection"},
        ],
    },
    {
        "id": "engineering",
        "label": "Best practices & engineering",
        "categories": [
            {"id": "best_practice", "label": "Best practices"},
            {"id": "architecture", "label": "Architecture"},
            {"id": "maintainability", "label": "Maintainability"},
            {"id": "readability", "label": "Readability"},
            {"id": "code_quality", "label": "Code quality"},
            {"id": "testing", "label": "Testing"},
            {"id": "documentation", "label": "Documentation"},
            {"id": "dependency_hygiene", "label": "Dependency hygiene"},
            {"id": "cognitive_complexity", "label": "Complexity"},
        ],
    },
    {
        "id": "runtime",
        "label": "Correctness & runtime",
        "categories": [
            {"id": "bug", "label": "Bugs"},
            {"id": "runtime", "label": "Runtime failures"},
            {"id": "logic", "label": "Logic errors"},
            {"id": "validation", "label": "Validation"},
            {"id": "input_validation", "label": "Input validation"},
            {"id": "null_check", "label": "Null safety"},
            {"id": "edge_case", "label": "Edge cases"},
            {"id": "business_logic", "label": "Business logic"},
            {"id": "idempotency", "label": "Idempotency"},
        ],
    },
    {
        "id": "data_perf",
        "label": "Data & performance",
        "categories": [
            {"id": "database", "label": "Database"},
            {"id": "data_integrity", "label": "Data integrity"},
            {"id": "data_lifecycle", "label": "Data lifecycle"},
            {"id": "performance", "label": "Performance"},
            {"id": "scalability", "label": "Scalability"},
            {"id": "concurrency", "label": "Concurrency"},
            {"id": "memory", "label": "Memory"},
            {"id": "api_design", "label": "API design"},
        ],
    },
    {
        "id": "product",
        "label": "Product & frontend",
        "categories": [
            {"id": "uiux", "label": "UI/UX"},
            {"id": "saas_completeness", "label": "SaaS completeness"},
            {"id": "ux_copy", "label": "User messaging"},
        ],
    },
]

# One-click bundles (ids must be valid IssueType values)
REVIEW_PRESETS: List[Dict[str, Any]] = [
    {
        "id": "production_grade",
        "label": "Production grade",
        "description": "Deploy safety, reliability, ops, and release readiness",
        "categories": [
            "production_readiness", "reliability", "error_handling", "observability",
            "configuration", "release_readiness", "rollback_confidence", "change_risk",
            "slo_impact", "ownership_runbook", "testing", "documentation",
        ],
    },
    {
        "id": "security_compliance",
        "label": "Security & compliance",
        "description": "Auth, tenancy, injection, secrets, compliance",
        "categories": [
            "security", "authentication", "authorization", "multi_tenancy", "gdpr",
            "compliance", "security_abuse", "sql_injection", "auth_hardening",
            "tls_ssl", "webhook_security", "client_secret", "upload_abuse", "csv_injection",
        ],
    },
    {
        "id": "best_practices",
        "label": "Best practices",
        "description": "Architecture, maintainability, tests, dependencies",
        "categories": [
            "best_practice", "architecture", "maintainability", "readability",
            "code_quality", "testing", "documentation", "dependency_hygiene",
        ],
    },
    {
        "id": "performance",
        "label": "Performance & scale",
        "description": "Latency, DB, concurrency, scalability",
        "categories": [
            "performance", "scalability", "database", "concurrency", "memory", "slo_impact",
        ],
    },
]


def all_category_ids() -> List[str]:
    ids: List[str] = []
    for group in REVIEW_CATEGORY_GROUPS:
        for cat in group["categories"]:
            ids.append(cat["id"])
    return ids


def categories_payload() -> Dict[str, Any]:
    return {
        "groups": REVIEW_CATEGORY_GROUPS,
        "presets": REVIEW_PRESETS,
        "all_ids": all_category_ids(),
    }


def serialize_focus_areas(focus_areas: Optional[List[IssueType]]) -> Optional[str]:
    if not focus_areas:
        return None
    return json.dumps([a.value if isinstance(a, IssueType) else str(a) for a in focus_areas])


def parse_focus_areas_json(raw: Optional[str]) -> Optional[List[IssueType]]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, list) or not data:
        return None
    out: List[IssueType] = []
    for item in data:
        try:
            out.append(IssueType(item))
        except ValueError:
            continue
    return out or None


def focus_areas_instruction(focus_areas: Optional[List[IssueType]]) -> str:
    if not focus_areas:
        return ""
    names = ", ".join(a.value for a in focus_areas)
    return (
        "\n**SCOPE LIMIT — REQUIRED:** Only report issues whose `type` is one of: "
        f"{names}.\n"
        "- Do NOT report issues outside these categories.\n"
        "- Apply the full depth of review, but stay strictly within this scope.\n"
        "- If nothing applies in scope, return an empty `issues` array.\n"
    )
