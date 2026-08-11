#!/usr/bin/env python3
"""Deterministic step planner for the main2main upgrade pipeline.

Splits a range of upstream vLLM commits into ordered steps based on changed
lines in vllm/ source files. Commits that do not touch vllm/ are skipped.

Algorithm:
  1. git log --reverse base..target → ordered commit list
  2. For each commit, git diff-tree --numstat → vllm/ changed lines
  3. Keep only commits that touch vllm/; skip others
  4. Commits accumulate into a step until vllm_changed_lines > LINE_BUDGET
     or the step reaches the commit-count budget
  5. A single commit with vllm_changed_lines > LINE_BUDGET becomes its own step

Overridable via env for tuning:
  MAIN2MAIN_LINE_BUDGET   (default 1000 vllm/ changed lines per step)
  MAIN2MAIN_COMMIT_BUDGET (default 20 commits per step)

Output:
  - <workspace>/steps.json  — machine-readable plan
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from main2main_flow.scripts.utils.utils import run_git, ts_print

LINE_BUDGET = int(os.environ.get("MAIN2MAIN_LINE_BUDGET", "1000"))
BASE_COMMIT_COUNT_BUDGET = int(os.environ.get("MAIN2MAIN_COMMIT_BUDGET", "20"))


# ── vllm-report MCP client ────────────────────────────────────────────────────

class _VllmReportMCPClient:
    """Long-lived stdio JSON-RPC client for vllm-report MCP server.

    Used by plan_steps to batch-query commit impact analysis, so the planner
    can route commits into adapt/no-op/fallback buckets without involving
    the adapter agent.
    """

    def __init__(self, vllm_report_path: Path, ascend_path: Path | None = None):
        cmd = [sys.executable, "-m", "src.mcp_server_app",
               "--data-dir", str(vllm_report_path / "data")]
        if ascend_path:
            cmd += ["--ascend-repo-path", str(ascend_path)]
        self.proc = subprocess.Popen(
            cmd, cwd=str(vllm_report_path),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1,
        )
        self._rpc_id = 0
        self._initialize()

    def _send(self, method: str, params: dict | None = None) -> dict:
        self._rpc_id += 1
        req = {"jsonrpc": "2.0", "method": method,
               "params": params or {}, "id": self._rpc_id}
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            err = self.proc.stderr.read() if self.proc.stderr else ""
            raise RuntimeError(f"MCP server closed: {err[:300]}")
        return json.loads(line)

    def _notify(self, method: str, params: dict | None = None) -> None:
        req = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()

    def _initialize(self) -> None:
        self._send("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "plan_steps", "version": "1"},
        })
        self._notify("notifications/initialized")

    def get_commit_impacts(self, shas: list[str]) -> list[dict]:
        if not shas:
            return []
        resp = self._send("tools/call", {
            "name": "get_commit_impact_batch",
            "arguments": {"shas": shas},
        })
        # Response: {"result": {"content": [{"type":"text","text":"<json>"}]}}
        result = resp.get("result", {})
        content = result.get("content", [])
        if not content:
            raise RuntimeError(f"MCP no content: {resp}")
        text = content[0].get("text", "")
        parsed = json.loads(text)
        return parsed.get("results", [])

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _resolve_impacts(shas: list[str], vllm_report_path: Path | None,
                     ascend_path: Path | None = None) -> dict[str, dict]:
    """Return sha → impact dict. None for shas that can't be resolved
    (MCP unavailable, server crash, or sha not in vllm-report index).

    A None impact tells the planner to fall back to the vllm-line-budget
    algorithm for that commit — same behavior as before this feature.
    """
    if not vllm_report_path or not (vllm_report_path / "data").exists():
        return {sha: None for sha in shas}
    impacts: dict[str, dict] = {sha: None for sha in shas}
    try:
        with _VllmReportMCPClient(vllm_report_path, ascend_path) as client:
            results = client.get_commit_impacts(shas)
            for r in results:
                sha = r.get("sha", "")
                if sha and r.get("analyzed"):
                    impacts[sha] = r
    except Exception as e:
        ts_print(f"[plan] vllm-report MCP unavailable, falling back to "
                 f"line-budget for all commits: {str(e)[:200]}")
    return impacts


# ── bucket classification ─────────────────────────────────────────────────────

def _bucket_of(impact: dict | None, vllm_lines: int) -> str:
    """'noop' / 'code' — see plan algorithm in module docstring."""
    if impact is None:
        # Unanalyzed: docs/CI-only → noop; code-touching → code
        return "noop" if vllm_lines == 0 else "code"
    if not impact.get("ascend_affected", False):
        return "noop"
    return "code"


# ── commit listing ─────────────────────────────────────────────────────────────

def _commit_count_budget() -> int:
    return max(1, BASE_COMMIT_COUNT_BUDGET)


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
    output = run_git(repo, "diff-tree", "--no-commit-id", "-r", "--numstat", sha, "--", ":(top)vllm/")
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


def _make_step(index: int, commits: list[dict[str, str]], start: str, lines: int,
               bucket: str, tags: list[str] | None = None) -> dict[str, Any]:
    return {
        "index": index,
        "id": f"step-{index}",
        "commits": list(commits),
        "commit_count": len(commits),
        "start_commit": start,
        "end_commit": commits[-1]["sha"],
        "vllm_changed_lines": lines,
        "line_budget": LINE_BUDGET,
        "commit_count_budget": _commit_count_budget(),
        "bucket": bucket,
        "tags": sorted(set(tags or [])),
        "noop": bucket == "noop",
    }


def _commit_count_budget() -> int:
    return max(1, BASE_COMMIT_COUNT_BUDGET)


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
    output = run_git(repo, "diff-tree", "--no-commit-id", "-r", "--numstat", sha, "--", ":(top)vllm/")
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


def _plan_steps(
    commits: list[dict[str, str]],
    lines_per_commit: dict[str, int],
    impacts: dict[str, dict | None],
    base_commit: str,
) -> list[dict[str, Any]]:
    """Group commits into steps by bucket type + line/commit budget.

    Bucket rules (per commit):
      - impact is None (unanalyzed) + vllm_lines == 0 → 'noop'
      - impact is None (unanalyzed) + vllm_lines > 0  → 'code'
      - impact.ascend_affected == False → 'noop'
      - impact.ascend_affected == True  → 'code'

    Within a bucket:
      - 'noop': consecutive commits merge into one no-op step (no adapter,
        only advances verified.commit).
      - 'code': current vllm-line-budget + commit-count-budget grouping
        (single commit > LINE_BUDGET gets its own step).

    When the bucket type changes, the current step closes — except a single
    noop commit with 0 vllm lines may merge into an open 'code' step (it
    only advances verified.commit there).  This avoids fragmenting runs of
    code-touching commits just because a docs-only commit sits between them.
    """
    # Track which commits have been placed (verifies no omissions).
    placed_shas: set[str] = set()
    steps: list[dict[str, Any]] = []

    # State for the currently-open step.
    cur_bucket: str | None = None
    cur_commits: list[dict[str, str]] = []
    cur_lines = 0
    cur_tags: list[str] = []
    start = base_commit

    def close_step() -> None:
        nonlocal cur_commits, cur_lines, cur_tags, cur_bucket, start
        if not cur_commits:
            return
        steps.append(_make_step(len(steps) + 1, cur_commits, start,
                                cur_lines, cur_bucket, cur_tags))
        for c in cur_commits:
            placed_shas.add(c["sha"])
        start = cur_commits[-1]["sha"]
        cur_commits = []
        cur_lines = 0
        cur_tags = []

    for commit in commits:
        sha = commit["sha"]
        lines = lines_per_commit.get(sha, 0)
        impact = impacts.get(sha)
        bucket = _bucket_of(impact, lines)
        commit_tags = (impact or {}).get("tags", []) or []

        # Bucket-type switch: close current step — EXCEPT a 'noop' commit
        # with 0 vllm lines may merge into an open 'fallback' or 'adapt'
        # step (it only advances verified.commit there, doesn't change the
        # work).  This avoids fragmenting runs of code-touching commits
        # just because a docs-only commit sits between them (29 commits
        # → 13 steps was too many; most steps had 1-2 commits).
        if cur_bucket is not None and bucket != cur_bucket:
            if bucket == "noop" and lines == 0 and cur_bucket == "code":
                # Merge this noop commit into the current step; keep
                # cur_bucket unchanged.
                cur_commits.append(commit)
                # No lines added (lines == 0).  No tags for noop.
                placed_shas.add(sha)
                # Continue to next commit without resetting bucket state.
                continue
            close_step()
            cur_bucket = bucket
            cur_tags = list(commit_tags)
        elif cur_bucket is None:
            cur_bucket = bucket
            cur_tags = list(commit_tags)

        # 'code' bucket (was 'adapt' + 'fallback'): use the current vllm-line
        # + commit-count budget.  No theme grouping — theme routing was too
        # granular and produced too many steps (29 commits → 13 steps).
        # The 1000-line budget is the only grouper.
        if bucket == "code":
            if lines > LINE_BUDGET:
                close_step()
                cur_commits.append(commit)
                cur_lines = lines
                close_step()
                continue
            if cur_lines + lines > LINE_BUDGET or len(cur_commits) >= _commit_count_budget():
                close_step()
        # 'noop' bucket: keep accumulating; no budget, no adapter.

        cur_commits.append(commit)
        cur_lines += lines
        # Tags track the union across the step (for code bucket, when impact
        # analysis is available — purely informational).
        if bucket == "code":
            for t in commit_tags:
                if t not in cur_tags:
                    cur_tags.append(t)

    close_step()

    # If all commits were filtered into a noop bucket at index 0, we still
    # have steps (no special case needed).  But if commits existed and
    # produced zero steps (shouldn't happen), fall back to a single no-op.
    if not steps and commits:
        steps.append(_make_step(1, commits, base_commit, 0, "noop", []))
        for c in commits:
            placed_shas.add(c["sha"])

    # Safety net: every input commit must appear in exactly one step.
    missing = [c["sha"] for c in commits if c["sha"] not in placed_shas]
    if missing:
        # This is a bug in the algorithm; create a fallback step for the
        # leftover commits so verified.commit still advances.
        ts_print(f"[plan] WARNING: {len(missing)} commits not placed in any "
                 f"step, creating fallback step: {[s[:8] for s in missing[:5]]}")
        steps.append(_make_step(len(steps) + 1,
                                [c for c in commits if c["sha"] in missing],
                                steps[-1]["end_commit"] if steps else base_commit,
                                0, "code", []))

    return steps


def _enrich_steps_with_diff(vllm_path: Path, steps: list[dict[str, Any]]) -> None:
    for step in steps:
        step["upstream_patch"] = run_git(
            vllm_path, "diff",
            f"{step['start_commit']}..{step['end_commit']}",
            "--", ":(top)vllm/",
        )
        changed_files = run_git(
            vllm_path, "diff", "--name-only",
            f"{step['start_commit']}..{step['end_commit']}",
            "--", ":(top)vllm/",
        )
        step["changed_files"] = changed_files
        step["files_changed"] = sorted(f for f in changed_files.strip().splitlines() if f)


def run_plan(vllm_path: Path, base_commit: str, target_commit: str,
             vllm_report_path: Path | None = None,
             ascend_path: Path | None = None) -> dict[str, Any]:
    commits = _list_commits(vllm_path, base_commit, target_commit)
    lines_per_commit = {c["sha"]: _vllm_lines_for_commit(vllm_path, c["sha"]) for c in commits}

    impacts = _resolve_impacts([c["sha"] for c in commits], vllm_report_path, ascend_path)
    analyzed = sum(1 for v in impacts.values() if v is not None)
    ts_print(f"[plan] vllm-report impact: {analyzed}/{len(commits)} commits analyzed, "
             f"{len(commits) - analyzed} fall back to line-budget")

    steps = _plan_steps(commits, lines_per_commit, impacts, base_commit)
    _enrich_steps_with_diff(vllm_path, steps)
    return {
        "base_commit": base_commit,
        "target_commit": target_commit,
        "total_commits": sum(s["commit_count"] for s in steps),
        "steps": steps,
    }