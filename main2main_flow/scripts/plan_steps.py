#!/usr/bin/env python3
"""Deterministic step planner for the main2main upgrade pipeline.

Splits a range of upstream vLLM commits into ordered steps based on changed
lines in vllm/ source files. Commits that do not touch vllm/ are skipped.
Pathspecs listed in MAIN2MAIN_PLAN_EXCLUDES (space-separated) are excluded
from both line counting and the generated patches.

Algorithm:
  1. git log --reverse base..target → ordered commit list
  2. For each commit, git diff-tree --numstat → vllm/ changed lines
  3. Keep only commits that touch vllm/; skip others
  4. Commits accumulate into a step until vllm_changed_lines > LINE_BUDGET
     or the step reaches COMMIT_COUNT_BUDGET commits
  5. A single commit with vllm_changed_lines > LINE_BUDGET becomes its own step

Output:
  - run_plan returns the machine-readable plan (written to <workspace>/steps.json
    by the flow). With steps_dir given, per-step upstream.patch and
    changed_files.txt are written there and the raw text is left out of the
    returned step dicts.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from main2main_flow.utils import (
    VLLM_GIT_CHANGED_FILES,
    VLLM_GIT_PATCH_FILE,
    run_git,
)

LINE_BUDGET = 1000
COMMIT_COUNT_BUDGET = 10


def _diff_pathspecs() -> list[str]:
    specs = [":(top)vllm/"]
    for p in os.getenv("MAIN2MAIN_PLAN_EXCLUDES", "").split():
        specs.append(":(exclude,top)" + p)
    return specs


def _list_commits(repo: Path, base: str, target: str) -> list[dict[str, str]]:
    log_output = run_git(
        repo, "log", "--reverse", "--format=%H%x1f%s", f"{base}..{target}"
    )
    commits: list[dict[str, str]] = []
    for line in log_output.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f", 1)
        commits.append({
            "sha": parts[0].strip(),
            "subject": parts[1].strip() if len(parts) > 1 else "",
        })
    return commits


def _vllm_lines_for_commit(repo: Path, sha: str) -> int:
    output = run_git(
        repo, "diff-tree", "--no-commit-id", "-r", "--numstat", sha,
        "--", *_diff_pathspecs(),
    )
    total = 0
    for line in output.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) >= 3:
            added = int(parts[0]) if parts[0] != "-" else 0
            deleted = int(parts[1]) if parts[1] != "-" else 0
            total += added + deleted
    return total


def _make_step(index: int, commits: list[dict[str, str]], start: str, lines: int) -> dict[str, Any]:
    return {
        "index": index,
        "id": f"step-{index}",
        "commits": list(commits),
        "commit_count": len(commits),
        "start_commit": start,
        "end_commit": commits[-1]["sha"],
        "vllm_changed_lines": lines,
        "line_budget": LINE_BUDGET,
        "commit_count_budget": COMMIT_COUNT_BUDGET,
    }


def _plan_steps(
    commits: list[dict[str, str]],
    lines_per_commit: dict[str, int],
    base_commit: str,
) -> list[dict[str, Any]]:
    eligible = [c for c in commits if lines_per_commit.get(c["sha"], 0) > 0]

    steps: list[dict[str, Any]] = []
    step_commits: list[dict[str, str]] = []
    step_lines = 0
    start = base_commit

    for commit in eligible:
        lines = lines_per_commit[commit["sha"]]

        if lines > LINE_BUDGET:
            if step_commits:
                steps.append(_make_step(len(steps) + 1, step_commits, start, step_lines))
                start = steps[-1]["end_commit"]
                step_commits = []
                step_lines = 0
            steps.append(_make_step(len(steps) + 1, [commit], start, lines))
            start = steps[-1]["end_commit"]
            continue

        if step_lines + lines > LINE_BUDGET or len(step_commits) >= COMMIT_COUNT_BUDGET:
            steps.append(_make_step(len(steps) + 1, step_commits, start, step_lines))
            start = steps[-1]["end_commit"]
            step_commits = []
            step_lines = 0

        step_commits.append(commit)
        step_lines += lines

    if step_commits:
        steps.append(_make_step(len(steps) + 1, step_commits, start, step_lines))

    return steps


def _enrich_steps_with_diff(
    vllm_path: Path,
    steps: list[dict[str, Any]],
    steps_dir: Path | None,
) -> None:
    specs = _diff_pathspecs()
    for step in steps:
        patch = run_git(
            vllm_path, "diff",
            f"{step['start_commit']}..{step['end_commit']}",
            "--", *specs,
        )
        changed_files = run_git(
            vllm_path, "diff", "--name-only",
            f"{step['start_commit']}..{step['end_commit']}",
            "--", *specs,
        )
        step["files_changed"] = sorted(f for f in changed_files.strip().splitlines() if f)
        if steps_dir is None:
            # legacy/standalone mode: embed the raw text in the plan
            step["upstream_patch"] = patch
            step["changed_files"] = changed_files
        else:
            step_dir = steps_dir / step["id"]
            step_dir.mkdir(parents=True, exist_ok=True)
            (step_dir / VLLM_GIT_PATCH_FILE).write_text(patch, encoding="utf-8")
            (step_dir / VLLM_GIT_CHANGED_FILES).write_text(changed_files, encoding="utf-8")


def run_plan(
    vllm_path: Path,
    base_commit: str,
    target_commit: str,
    steps_dir: Path | None = None,
) -> dict[str, Any]:
    """Plan the upgrade steps for base_commit..target_commit.

    With ``steps_dir`` given, per-step upstream.patch and changed_files.txt are
    written under ``<steps_dir>/<step-id>/`` and the returned step dicts carry
    no raw patch text (only the ``files_changed`` list). With ``steps_dir=None``
    the raw ``upstream_patch``/``changed_files`` text is embedded (legacy mode).
    """
    commits = _list_commits(vllm_path, base_commit, target_commit)
    lines_per_commit = {c["sha"]: _vllm_lines_for_commit(vllm_path, c["sha"]) for c in commits}

    steps = _plan_steps(commits, lines_per_commit, base_commit)
    _enrich_steps_with_diff(vllm_path, steps, steps_dir)
    return {
        "base_commit": base_commit,
        "target_commit": target_commit,
        "total_commits": sum(s["commit_count"] for s in steps),
        "steps": steps,
    }
