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
    """Collect (test_name, error_text) from the step's e2e rounds.

    The lesson is recorded when the step PASSES after a fix round — but the
    failure details that prompted the fix live in the EARLIER rounds
    (round-0 is the first attempt, which the old code skipped).  Read every
    round and keep the FIRST failure per test: round-0's traceback is the
    original bug, later failed rounds are the adapter's attempts at it.
    """
    failures: list[tuple[str, str]] = []
    seen: set[str] = set()
    if not tests_dir.is_dir():
        return failures
    for rfile in sorted(tests_dir.glob("round-*-result.json")):
        try:
            data = json.loads(rfile.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for test_name, tr in (data.get("suite_results") or {}).items():
            if tr.get("ci_result") in ("passed", "env_flake_pass"):
                continue
            if test_name in seen:
                continue  # keep the earliest round's failure
            seen.add(test_name)
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


def _submit_via_mcp(report_dir: Path, *, title: str, symptom: str,
                    root_cause: str, fix_guidance: list[str], tags: list[str],
                    keywords: list[str] | None = None,
                    example: str | None = None) -> None:
    """Submit a lesson through vllm-report's OWN MCP ``submit_lesson``
    implementation (same function the adapter's MCP call dispatches to).
    ``submit_lesson`` commits + pushes, so the lesson is persisted
    immediately — the clone is re-created every run."""
    import sys as _sys
    import asyncio
    _sys.path.insert(0, str(report_dir))
    try:
        import src.mcp_server_app as mcp_app
        mcp_app.data_dir = str(report_dir / "data")
    except Exception as e:
        ts_print(f"[lesson] cannot import vllm-report MCP module "
                 f"({e}), skipping lesson")
        return
    try:
        result = asyncio.run(mcp_app.tool_submit_lesson(
            title=title,
            symptom=symptom,
            root_cause=root_cause,
            fix_guidance=fix_guidance,
            tags=tags,
            keywords=keywords,
            example=example,
        ))
        ts_print(f"[lesson] submit_lesson → {result}")
    except Exception as e:
        ts_print(f"[lesson] failed to submit lesson: {e}")


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
    _submit_via_mcp(report_dir, title=title, symptom=symptom,
                    root_cause=root_cause, fix_guidance=fix_guidance,
                    tags=["e2e-fix", "auto-submitted"],
                    keywords=keywords, example=example)


def submit_gate_lesson(vllm_report_path: str, error_logs: list[str]) -> None:
    """Record a lesson when the final quality gate needed a fix round.

    The gate's adapter-fix loop resolves UT/format/mypy failures found only
    at the end of the run (e.g. version-dependent UT failures, test
    isolation issues) — knowledge as valuable as per-step E2E lessons, and
    previously lost.  Called by flow._final_quality_gate when a fix round
    succeeded.
    """
    if not vllm_report_path or not error_logs:
        return
    report_dir = Path(vllm_report_path)
    failed = [l for l in error_logs if "FAILED" in l or "ERROR" in l]
    error_text = "\n".join(error_logs)
    keywords = _extract_keywords(error_text, "final quality gate failure")
    title = (f"final-gate: fix needed "
             f"({len(failed) or len(error_logs)} failure(s))")
    symptom = ("Final quality gate (format+mypy+UT) failed: "
               + "; ".join((failed or error_logs)[:3]))
    root_cause = ("The gate found failures that per-step e2e did not — "
                  "version-dependent UT expectations (vllm_version_is), "
                  "test isolation/mock requirements, or format/mypy issues "
                  "surfacing only on the cumulative state.")
    fix_guidance = [
        "Check if the failing UT needs a vllm_version_is('0.26.0') branch "
        "or a version guard",
        "Check if the failing UT constructs objects requiring ascend config "
        "or NPU env — add a mock/fixture instead of relying on isolation",
        "Verify the fix against the failing test specifically",
    ]
    example = error_text[:200]
    _submit_via_mcp(report_dir, title=title, symptom=symptom,
                    root_cause=root_cause, fix_guidance=fix_guidance,
                    tags=["final-gate", "auto-submitted"],
                    keywords=keywords, example=example)


def _resolve_push_targets(report_dir: Path) -> list[str]:
    """Push targets for the vllm-report clone, honoring the runner's own
    github rewrite (``url.*.insteadOf``): embed the token in the REWRITTEN
    base so the push goes through the same proxy the runner uses for fetch
    (git-cdn in CI, none locally) — the pattern that made
    ``_push_via_proxy`` (push_to_github.py) work.  Falls back to the CI
    push proxy and direct github.com (local runs / credential helper).
    """
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or ""
    repo_url = "https://github.com/vllm-ascend/vllm-report.git"
    targets: list[str] = []
    if token:
        # 1. The runner's own github proxy (git config url.*.insteadOf),
        #    e.g. http://git-cdn-service...:8000 — reachable wherever the
        #    clone's fetch worked, and the only proxy that survives on
        #    runners without gh-proxy access.  Without this the push fell
        #    through to `origin`, got rewritten to the proxy, and failed
        #    with "could not read Username" (run 31563761175).
        r = subprocess.run(
            ["git", "config", "--get-regexp", r"^url\..*\.insteadof$"],
            cwd=str(report_dir), capture_output=True, text=True)
        for line in r.stdout.splitlines():
            key, _, value = line.partition(" ")
            if "github.com" not in value:
                continue
            base = key[len("url."):-len(".insteadof")]
            scheme, _, rest = base.partition("://")
            targets.append(
                f"{scheme}://x-access-token:{token}@{rest}{repo_url}")
            break
        # 2. The CI push proxy used by push_to_github._push_via_proxy.
        targets.append(
            f"https://x-access-token:{token}@gh-proxy.test.osinfra.cn/"
            f"{repo_url}")
        # 3. Direct github.com (local runs, non-proxy runners).
        targets.append(f"https://x-access-token:{token}@{repo_url[8:]}")
    targets.append("origin")
    return targets


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
    # Also count unpushed commits: vllm-report's tool_submit_lesson commits
    # locally BEFORE pushing, so a failed submit push leaves a clean working
    # tree with an unpushed commit that `git status` cannot see (run
    # 31563761175: "no vllm-report changes to persist" while the lesson
    # commit was stranded).
    ahead = subprocess.run(
        ["git", "rev-list", "--count", "origin/main..HEAD"],
        cwd=str(report_dir), capture_output=True, text=True)
    ahead_count = int(ahead.stdout.strip()) if ahead.stdout.strip().isdigit() else 0
    if r.returncode != 0 or (not r.stdout.strip() and ahead_count == 0):
        ts_print("[lesson] no vllm-report changes to persist")
        return
    # The fresh clone has no git identity — a bare `git commit` fails with
    # "Author identity unknown" (exit 128).  Set it per-command.
    identity = ["-c", "user.name=main2main-bot",
                "-c", "user.email=main2main-bot@users.noreply.github.com"]
    try:
        if r.stdout.strip():
            subprocess.run(["git", *identity, "add", "-A"],
                           cwd=str(report_dir), check=True, capture_output=True,
                           text=True)
            commit_msg = (f"lessons: {datetime.now().strftime('%Y-%m-%d %H:%M')} "
                          "auto-recorded from main2main E2E fixes")
            subprocess.run(["git", *identity, "commit", "-m", commit_msg],
                           cwd=str(report_dir), check=True, capture_output=True,
                           text=True)
        # Fetch + rebase before push: the daily data-update bot commits to
        # main between our clone and this push, so a bare push would be
        # rejected as non-fast-forward (mirrors vllm-report's
        # _persist_lesson_to_remote).  Fetch through the first target so it
        # goes through the runner's own proxy.
        targets = _resolve_push_targets(report_dir)
        fr = subprocess.run(["git", "fetch", targets[0], "main"],
                            cwd=str(report_dir), capture_output=True,
                            text=True)
        if fr.returncode == 0:
            rb = subprocess.run(["git", "rebase", "FETCH_HEAD"],
                                cwd=str(report_dir), capture_output=True,
                                text=True)
            if rb.returncode != 0:
                subprocess.run(["git", "rebase", "--abort"],
                               cwd=str(report_dir), capture_output=True,
                               text=True)
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
