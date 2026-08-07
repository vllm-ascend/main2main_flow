#!/usr/bin/env python3
"""Record adaptation lessons to vllm-report and persist them to the remote.

When a step's adaptation needed >=1 E2E fix round (the first attempt
missed a case the E2E test covers), we record the failure as a lesson in
vllm-report's knowledge base — a long-term asset.  Future runs query it
via the MCP ``get_adaptation_lessons`` tool and fix the same failure in
one pass instead of repeating the fix rounds.

Data model (mirrors vllm-report's MCP submit_lesson tool):
  data/vllm-ascend/lessons/<date>.json
  {
    "date": "2026-08-06",
    "lessons": [
      {
        "id": "L20260806-001",
        "title": "...",
        "keywords": [...],
        "symptom": "...",
        "root_cause": "...",
        "fix_guidance": [...],
        "tags": [...],
        "example": "...",
        "created_at": "...",
        "hits": 0
      }
    ]
  }

Only SUCCESSFUL fixes are recorded (E2E passed after >=1 fix round).
The vllm-report clone is re-created every run (flow.initialize does
rmtree + clone), so persist_lessons() must commit + push before the
run ends — otherwise the lessons are lost.
"""
from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path

from main2main_flow.scripts.utils.utils import (
    WORKSPACE_DIR,
    STEPS_DIR,
    ts_print,
)

# Mirrors vllm-report's MCP submit_lesson keyword-extraction heuristic:
# take the first non-empty line of the error (usually
# "AssertionError: Failed to apply..." -> "Failed to apply...").
_KEYWORD_MIN_LEN = 8
_KEYWORD_MAX_LEN = 120


def _collect_failures(tests_dir: Path) -> list[tuple[str, str]]:
    """Collect (test_name, error_text) from round-N-result.json fix rounds.

    round-0 is the first attempt (not a fix round) — skipped.  Later
    rounds that still failed are the ones the adapter had to fix.
    """
    failures: list[tuple[str, str]] = []
    if not tests_dir.is_dir():
        return failures
    for rfile in sorted(tests_dir.glob("round-*-result.json")):
        if "round-0" in rfile.name:
            continue  # first attempt, not a fix round
        try:
            data = json.loads(rfile.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for test_name, tr in (data.get("suite_results") or {}).items():
            if tr.get("ci_result") in ("passed", "env_flake_pass"):
                continue
            error_text = ""
            for bug in (tr.get("code_bugs") or []):
                error_text += (bug.get("traceback") or bug.get("error")
                               or "")[:400]
            if not error_text:
                error_text = (tr.get("error") or tr.get("summary")
                              or "")[:400]
            failures.append((test_name, error_text))
    return failures


def _extract_keywords(error_text: str, fallback: str) -> list[str]:
    """Extract search keywords from an error message (first lines)."""
    keywords: list[str] = []
    for ln in error_text.splitlines():
        ln = ln.strip()
        if not ln:
            continue
        # "AssertionError: Failed to apply..." -> "Failed to apply..."
        key = ln.split(":", 1)[-1].strip() if ":" in ln else ln
        if _KEYWORD_MIN_LEN <= len(key) <= _KEYWORD_MAX_LEN:
            keywords.append(key)
        if len(keywords) >= 3:
            break
    if not keywords:
        keywords = [fallback[:80]]
    return keywords


def submit_step_lesson(vllm_report_path: str, step_id: str) -> None:
    """Record a lesson when the step needed >=1 E2E fix round.

    Called by flow.process_steps when the step passes E2E after
    retry_count >= 1.  Reads the failed test details from the step's
    tests/ dir, extracts keywords + fixed files, then submits the lesson
    through vllm-report's OWN MCP ``submit_lesson`` implementation (the
    same function the adapter's MCP call dispatches to).  Since
    ``submit_lesson`` now commits + pushes to the remote, the lesson is
    persisted immediately — the clone is re-created every run.
    """
    if not vllm_report_path:
        return
    report_dir = Path(vllm_report_path)
    tests_dir = WORKSPACE_DIR / STEPS_DIR / step_id / "tests"

    failures = _collect_failures(tests_dir)
    if not failures:
        ts_print(f"[lesson] {step_id}: no E2E failure details found, "
                 "skipping lesson")
        return

    error_text = failures[0][1]
    keywords = _extract_keywords(error_text, failures[0][0].split("::")[-1])

    # Import vllm-report's MCP module from the cloned repo (same code path
    # the adapter uses via MCP) so id assignment / file format / persistence
    # live in one place.  data_dir is normally set by its main(); set it
    # here since we call the tool function directly.
    import sys as _sys
    import asyncio
    _sys.path.insert(0, str(report_dir))
    try:
        import src.mcp_server_app as mcp_app
        mcp_app.data_dir = str(report_dir / "data")
    except Exception as e:
        ts_print(f"[lesson] {step_id}: cannot import vllm-report MCP module "
                 f"({e}), skipping lesson")
        return

    title = f"{step_id}: E2E fix needed {failures[0][0].split('::')[-1][:60]}"
    symptom = (f"E2E failed on {len(failures)} test(s) in {step_id}: "
               f"{', '.join(t.split('::')[-1] for t, _ in failures[:3])}")
    root_cause = ("E2E failure required adapter-fix round(s) — the "
                  "initial adaptation missed a case the E2E test covers. "
                  "See the failed test log for the exact path.")
    fix_guidance = [
        "Read the E2E traceback to identify the exact failing path",
        "Check all code paths reaching the asserted invariant (normal "
        "vs cache, with-data vs no-data) — a fix covering only one "
        "path leaves E2E failing",
        "Verify the fix against the failing test specifically",
    ]
    example = f"Failed tests: {failures[0][0]} — {error_text[:200]}"
    try:
        result = asyncio.run(mcp_app.tool_submit_lesson(
            title=title,
            symptom=symptom,
            root_cause=root_cause,
            fix_guidance=fix_guidance,
            tags=["e2e-fix", "auto-submitted"],
            keywords=keywords,
            example=example,
        ))
        ts_print(f"[lesson] {step_id}: submit_lesson → {result}")
    except Exception as e:
        ts_print(f"[lesson] {step_id}: failed to submit lesson: {e}")


def persist_lessons(vllm_report_path: str) -> None:
    """Commit + push vllm-report lessons back to the remote (best-effort).

    Safety net for any data written to the clone this run (lessons already
    push via submit_lesson; this also covers e.g. adaptation-status writes
    and anything that failed to push at submit time).  The clone is
    re-created every run, so un-pushed changes are lost.
    """
    if not vllm_report_path:
        return
    report_dir = Path(vllm_report_path)
    lessons_dir = report_dir / "data" / "vllm-ascend" / "lessons"
    if not lessons_dir.is_dir():
        return

    r = subprocess.run(
        ["git", "status", "--short"], cwd=str(report_dir),
        capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        ts_print("[lesson] no vllm-report changes to persist")
        return
    # The fresh clone has no git identity — a bare `git commit` fails with
    # "Author identity unknown" (exit 128).  Set it per-command.
    identity = ["-c", "user.name=main2main-bot",
                "-c", "user.email=main2main-bot@users.noreply.github.com"]
    try:
        subprocess.run(["git", *identity, "add", "-A"],
                       cwd=str(report_dir), check=True, capture_output=True,
                       text=True)
        commit_msg = (f"lessons: {datetime.now().strftime('%Y-%m-%d %H:%M')} "
                      "auto-recorded from main2main E2E fixes")
        subprocess.run(["git", *identity, "commit", "-m", commit_msg],
                       cwd=str(report_dir), check=True, capture_output=True,
                       text=True)
        # Push with a token-embedded URL when a token is available: the CI
        # runner routes github.com through an anonymous-fetch proxy that
        # needs the token in the URL for push.  Fall back to the plain
        # origin push locally (credential helper).
        token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
        targets: list[str] = []
        if token:
            targets.append(
                f"https://x-access-token:{token}@gh-proxy.test.osinfra.cn/"
                f"https://github.com/vllm-ascend/vllm-report.git")
            targets.append(
                f"https://x-access-token:{token}@github.com/"
                f"vllm-ascend/vllm-report.git")
        targets.append("origin")
        last_err = ""
        for target in targets:
            pr = subprocess.run(["git", "push", target, "main"],
                                cwd=str(report_dir), capture_output=True,
                                text=True)
            if pr.returncode == 0:
                ts_print("[lesson] pushed vllm-report lessons to remote")
                return
            last_err = pr.stderr.strip()[:300]
        ts_print(f"[lesson] WARNING failed to push vllm-report lessons: "
                 f"{last_err}")
    except subprocess.CalledProcessError as e:
        ts_print(f"[lesson] WARNING failed to persist lessons: {e}")
