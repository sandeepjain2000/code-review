"""
scanner.py  v1.0.0
Standalone CLI tool: scan a folder and send all source files to CodeSentinel for review.

Usage:
    python scanner.py /path/to/project
    python scanner.py /path/to/project --max-files 30 --output report.json
    python scanner.py /path/to/project --api-url http://localhost:8000

Follows script-conventions: clear screen, banner, log file, summary report, exit codes.
"""

import os
import sys
import json
import time
import asyncio
import argparse
import traceback
from datetime import datetime
from pathlib import Path

# ── Script identity ──────────────────────────────────────────────────────────
SCRIPT_NAME = "scanner.py"
VERSION = "1.0.0"
start_time = datetime.now()

# ── Setup output directory ────────────────────────────────────────────────────
output_dir = Path(__file__).parent / "logs_and_reports"
output_dir.mkdir(exist_ok=True)

ts = start_time.strftime("%Y%m%d_%H%M%S")
log_path    = output_dir / f"scanner_{ts}.log"
report_path = output_dir / f"scanner_report_{ts}.txt"
log_file    = open(log_path, "w", encoding="utf-8")

# ── Helpers ───────────────────────────────────────────────────────────────────
def log(msg: str = "") -> None:
    """Print to console and write to log file."""
    print(msg)
    log_file.write(msg + "\n")
    log_file.flush()

def banner() -> None:
    os.system("cls" if os.name == "nt" else "clear")
    log("=" * 65)
    log(f"  {SCRIPT_NAME}  v{VERSION}")
    log(f"  Started : {start_time.strftime('%Y-%m-%d %H:%M:%S')}")
    log(f"  Log     : {log_path}")
    log("=" * 65)
    log()

def write_report(outcome: str, summary_lines: list, errors: list) -> None:
    end_time = datetime.now()
    duration = (end_time - start_time).total_seconds()
    lines = [
        "=" * 65,
        f"  SCRIPT REPORT: {SCRIPT_NAME}  v{VERSION}",
        "=" * 65,
        f"  Start Time : {start_time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"  End Time   : {end_time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"  Duration   : {duration:.1f} seconds",
        f"  Outcome    : {outcome}",
        "-" * 65,
        "  Summary:",
    ]
    for line in summary_lines:
        lines.append(f"    {line}")
    lines += [
        "-" * 65,
        "  Errors:" if errors else "  Errors: None",
    ]
    for e in errors:
        lines.append(f"    {e}")
    lines.append("=" * 65)

    report_text = "\n".join(lines)
    report_path.write_text(report_text, encoding="utf-8")
    log()
    log(report_text)
    log(f"\nReport saved → {report_path}")


# ── Core logic ────────────────────────────────────────────────────────────────
async def run_scan(args) -> tuple[str, list, list]:
    """
    Main async worker.
    Returns (outcome, summary_lines, errors)
    """
    import httpx

    api_url = args.api_url.rstrip("/")
    folder  = Path(args.folder).resolve()
    errors  = []
    summary = []

    log(f"  Folder   : {folder}")
    log(f"  API      : {api_url}")
    log(f"  Max files: {args.max_files}")
    log(f"  Recursive: {not args.no_recursive}")
    log()

    if not folder.exists():
        errors.append(f"Folder not found: {folder}")
        return "FAILURE", summary, errors

    # ── Build config from api_key.json ───────────────────────────────────────
    config_candidates = [
        folder / "api_key.json",
        Path(__file__).parent / "api_key.json",
        Path(__file__).parent / "backend" / "api_key.json",
    ]
    config = {}
    for cp in config_candidates:
        if cp.exists():
            with open(cp, encoding="utf-8") as f:
                config = json.load(f)
            log(f"  Config   : {cp}")
            break

    # ── Check server health ──────────────────────────────────────────────────
    log("Checking server health…")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{api_url}/health")
            resp.raise_for_status()
            health = resp.json()
            log(f"  ✓ Server OK — model: {health.get('model', 'unknown')}")
    except Exception as e:
        errors.append(f"Server health check failed: {e}")
        log(f"  ✗ Cannot reach {api_url}/health — is the backend running?")
        return "FAILURE", summary, errors

    log()
    log("Submitting folder for review…")

    # ── POST to /review/folder ───────────────────────────────────────────────
    payload = {
        "folder_path": str(folder),
        "recursive": not args.no_recursive,
        "max_files": args.max_files,
        "context": args.context or None,
    }

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            resp = await client.post(f"{api_url}/review/folder", json=payload)
            resp.raise_for_status()
            result = resp.json()
    except httpx.HTTPStatusError as e:
        errors.append(f"API error {e.response.status_code}: {e.response.text[:300]}")
        return "FAILURE", summary, errors
    except Exception as e:
        errors.append(f"Request failed: {e}")
        return "FAILURE", summary, errors

    elapsed = time.monotonic() - t0

    # ── Display results ──────────────────────────────────────────────────────
    files    = result.get("files", [])
    total_issues = result.get("total_issues", 0)
    overall_score = result.get("overall_score")
    sev = result.get("issues_by_severity", {})

    log()
    log("─" * 65)
    log(f"  Review ID   : {result.get('review_id', 'N/A')}")
    log(f"  Files       : {len(files)}")
    log(f"  Total issues: {total_issues}")
    log(f"  Score       : {overall_score}/100" if overall_score else "  Score       : N/A")
    log(f"  Time taken  : {elapsed:.1f}s")
    if sev:
        log()
        log("  Issues by severity:")
        for s in ["critical", "high", "medium", "low"]:
            if sev.get(s):
                icons = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🔵"}
                log(f"    {icons[s]}  {s.upper():10s} {sev[s]}")
    log()

    # Per-file breakdown
    for fr in files:
        file_issues = fr.get("issues", [])
        score = fr.get("score", "?")
        log(f"  📄 {fr['filename']}  [{len(file_issues)} issue(s), score: {score}]")
        for issue in file_issues:
            line_info = f"L{issue['line']}" if issue.get("line") else "file"
            log(f"     • [{issue['severity'].upper()}] {line_info}: {issue['message'][:80]}")
    log()

    # ── Save JSON output ─────────────────────────────────────────────────────
    output_file = args.output or str(output_dir / f"review_{ts}.json")
    Path(output_file).write_text(json.dumps(result, indent=2), encoding="utf-8")
    log(f"  Full JSON saved → {output_file}")

    summary = [
        f"Folder scanned: {folder}",
        f"Files reviewed: {len(files)}",
        f"Total issues found: {total_issues}",
        f"Critical: {sev.get('critical', 0)}, High: {sev.get('high', 0)}, "
        f"Medium: {sev.get('medium', 0)}, Low: {sev.get('low', 0)}",
        f"Overall score: {overall_score}/100" if overall_score else "Score: N/A",
        f"Review time: {elapsed:.1f}s",
        f"Results: {output_file}",
    ]

    return "SUCCESS", summary, []


# ── Entry point ───────────────────────────────────────────────────────────────
def parse_args():
    parser = argparse.ArgumentParser(
        description="CodeSentinel CLI Scanner — review an entire folder tree",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scanner.py ./my-project
  python scanner.py ./my-project --max-files 20
  python scanner.py ./my-project --output results.json --context "Django REST API"
  python scanner.py ./my-project --api-url http://localhost:8000
        """,
    )
    parser.add_argument("folder", help="Path to the folder to scan")
    parser.add_argument("--api-url", default="http://localhost:8000", help="CodeSentinel backend URL")
    parser.add_argument("--max-files", type=int, default=50, help="Max files to review (default: 50)")
    parser.add_argument("--no-recursive", action="store_true", help="Don't recurse into subdirectories")
    parser.add_argument("--context", help="Optional context about the codebase (e.g. 'Flask REST API')")
    parser.add_argument("--output", help="Path to save JSON results (default: logs_and_reports/review_<ts>.json)")
    return parser.parse_args()


def main():
    args = parse_args()
    banner()

    try:
        outcome, summary, errors = asyncio.run(run_scan(args))
    except KeyboardInterrupt:
        outcome = "ABORTED"
        summary = ["Interrupted by user"]
        errors  = ["KeyboardInterrupt"]
    except Exception as e:
        err = f"FATAL: {e}\n{traceback.format_exc()}"
        log(err)
        outcome = "FAILURE"
        summary = []
        errors  = [str(e)]

    write_report(outcome, summary, errors)

    log_file.close()
    sys.exit(0 if outcome == "SUCCESS" else 1)


if __name__ == "__main__":
    main()
