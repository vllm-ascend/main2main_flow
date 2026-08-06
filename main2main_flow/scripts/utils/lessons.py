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
import re
import subprocess
from datetime import datetime
from pathlib import Path

from main2main_flow.scripts.utils.utils import (
    WORKSPACE_DIR,
    STEPS_DIR,
    EACH_STEP_SUMMARY_FILE,
    run_git,
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


def _extract_fix_files(report_root: Path, step_id: str) -> list[str]:
    """Files this step's fix touched (from step_summary.md's adapted line)."""
    ssp = report_root / STEPS_DIR / step_id / EACH_STEP_SUMMARY_FILE
    if not ssp.exists():
        return []
    ssp_text = ssp.read_text(encoding="utf-8")
    pattern = rf"^- {re.escape(step_id)}: Adapted — (.+)$"
    m = re.search(pattern, ssp_text, re.M)
    if not m:
        return []
    return sorted(set(f.strip() for f in m.group(1).split(",") if f.strip()))[:5]


def submit_step_lesson(vllm_report_path: str, step_id: str) -> None:
    """Record a lesson when the step needed >=1 E2E fix round.

    Called by flow.process_steps when the step passes E2E after
    retry_count >= 1.  Reads the failed test details from the step's
    tests/ dir, extracts keywords + fixed files, and appends a lesson to
    vllm-report's lessons/<date>.json.
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
    fix_files = _extract_fix_files(WORKSPACE_DIR, step_id)

    today = datetime.now().strftime("%Y-%m-%d")
    lessons_dir = report_dir / "data" / "vllm-ascend" / "lessons"
    lessons_dir.mkdir(parents=True, exist_ok=True)
    fpath = lessons_dir / f"{today}.json"

    data = {}
    if fpath.exists():
        try:
            data = json.loads(fpath.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
    lessons = data.setdefault("lessons", [])
    seq = len(lessons) + 1
    lesson = {
        "id": f"L{today.replace('-', '')}-{seq:03d}",
        "title": (f"{step_id}: E2E fix needed "
                  f"{failures[0][0].split('::')[-1][:60]}"),
        "keywords": keywords,
        "symptom": (f"E2E failed on {len(failures)} test(s) in {step_id}: "
                    f"{', '.join(t.split('::')[-1] for t, _ in failures[:3])}"),
        "root_cause": "E2E failure required adapter-fix round(s) — the "
                      "initial adaptation missed a case the E2E test covers. "
                      "See the failed test log for the exact path.",
        "fix_guidance": [
            "Read the E2E traceback to identify the exact failing path",
            "Check all code paths reaching the asserted invariant (normal "
            "vs cache, with-data vs no-data) — a fix covering only one "
            "path leaves E2E failing",
            "Verify the fix against the failing test specifically",
        ],
        "tags": ["e2e-fix", "auto-submitted"],
        "example": f"Failed tests: {failures[0][0]} — {error_text[:200]}",
        "created_at": datetime.now().isoformat(),
        "hits": 0,
    }
    if fix_files:
        lesson["fix_files"] = fix_files
    lessons.append(lesson)
    fpath.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                     encoding="utf-8")
    ts_print(f"[lesson] {step_id}: recorded lesson {lesson['id']} "
             f"({len(failures)} E2E failure(s)) → "
             f"{fpath.relative_to(report_dir)}")


def persist_lessons(vllm_report_path: str) -> None:
    """Commit + push vllm-report lessons back to the remote (best-effort).

    The vllm-report clone is re-created every run, so lessons recorded
    this run are lost unless pushed.  Failures only log a warning.
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
    try:
        run_git(report_dir, "add", "-A")
        commit_msg = (f"lessons: {datetime.now().strftime('%Y-%m-%d %H:%M')} "
                      "auto-recorded from main2main E2E fixes")
        run_git(report_dir, "commit", "-m", commit_msg)
        subprocess.run(["git", "push", "origin", "main"],
                       cwd=str(report_dir), capture_output=True, text=True)
        ts_print("[lesson] pushed vllm-report lessons to remote")
    except subprocess.CalledProcessError as e:
        ts_print(f"[lesson] WARNING failed to persist lessons: {e}")
