#!/usr/bin/env python3
"""Deterministic step planner for the main2main upgrade pipeline.

Splits a range of upstream vLLM commits into ordered steps. Every commit
goes into a step (so verified.commit advances across the full range); the
only question is where the step boundaries fall.

Algorithm (user decision 2026-10-14, after run 38512795089 planned 17
steps for one run's backlog of 191 commits — each step pays a full e2e+UT
validation round, so step count dominates wall-clock):
  1. git log --reverse base..target → ordered commit list, each stamped
     with the UTC calendar day of its committer epoch
  2. Group consecutive commits by that day (one bucket per calendar day)
  3. A day whose total effective lines fit LINE_BUDGET and whose commit
     count fits COMMIT_BUDGET becomes one step; otherwise the day is split
     into exactly MAX_STEPS_PER_DAY steps with an even effective-line share

A day is never split into more than MAX_STEPS_PER_DAY steps regardless of
volume — the cost is coarser failure bisection (a failing step pins the
regression to half a day of commits instead of a ~2000-line slice), which
the user accepted for the wall-clock win.  Days stay capped rather than
merging the whole range: a single step spanning several days of upstream
PRs is too large for the adapter to land in one round.

The UTC stamping exists because a step must be a contiguous commit range
(it is applied as one diff) while upstream committer timezones interleave
the --date=short rendering along the topological order — the first cut of
this cap fragmented a 5-day backlog into 24 steps that bounced between
dates.

Overridable via env for tuning:
  MAIN2MAIN_LINE_BUDGET       (default 2000 vllm/ changed lines; the
                              1-step-vs-2 threshold within a day)
  MAIN2MAIN_COMMIT_BUDGET     (default 35 commits; ditto)
  MAIN2MAIN_MAX_STEPS_PER_DAY (default 2)

Output:
  - <workspace>/steps.json  — machine-readable plan
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from main2main_flow.scripts.utils.utils import run_git, ts_print

LINE_BUDGET = int(os.environ.get("MAIN2MAIN_LINE_BUDGET", "2000"))
BASE_COMMIT_COUNT_BUDGET = int(os.environ.get("MAIN2MAIN_COMMIT_BUDGET", "35"))
MAX_STEPS_PER_DAY = int(os.environ.get("MAIN2MAIN_MAX_STEPS_PER_DAY", "2"))


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

def _effective_lines(impact: dict | None, vllm_lines: int) -> int:
    """Lines that count toward the LINE_BUDGET step budget.

    A commit analyzed by vllm-report with ascend_affected=False contributes
    0 effective lines (plan still includes it in a step so verified.commit
    advances, but it doesn't consume budget).  Unanalyzed commits and
    ascend_affected=True commits contribute their full vllm/ line count.
    """
    if impact is not None and not impact.get("ascend_affected", False):
        return 0
    return vllm_lines


def _step_needs_adaptation(step_commits: list[dict[str, str]],
                           impacts: dict[str, dict | None]) -> bool:
    """Unused. The adapter always runs on every step and decides itself
    whether to skip e2e (via is_noop).  Kept only because the step dict
    records it for telemetry."""
    for c in step_commits:
        impact = impacts.get(c["sha"])
        if impact is None:
            return True
        if impact.get("ascend_affected", False):
            return True
    return False


# ── commit listing ─────────────────────────────────────────────────────────────

def _commit_count_budget() -> int:
    return max(1, BASE_COMMIT_COUNT_BUDGET)


def _list_commits(repo: Path, base: str, target: str) -> list[dict[str, str]]:
    # Day buckets must come from the committer EPOCH rendered in a fixed
    # timezone, not from the commit's own recorded offset: upstream vLLM
    # committers span timezones, and --date=short renders each commit in
    # its own offset, so calendar dates interleave along the topological
    # order and consecutive-run day grouping fragments the plan (run
    # 38512795089 attempt 2: a 5-day backlog split into 17 day-fragments
    # → 24 steps, worse than the linear planner it replaced).  UTC is
    # deterministic (independent of the runner's TZ) and monotonic with
    # git log --reverse's epoch ordering.
    log_output = run_git(
        repo, "log", "--reverse",
        "--format=%H%x1f%s%x1f%ct", f"{base}..{target}"
    )
    commits: list[dict[str, str]] = []
    for line in log_output.strip().splitlines():
        if not line.strip():
            continue
        parts = line.split("\x1f", 2)
        day = ""
        if len(parts) > 2 and parts[2].strip().isdigit():
            day = datetime.fromtimestamp(int(parts[2]), tz=timezone.utc)\
                .strftime("%Y-%m-%d")
        commits.append({
            "sha": parts[0].strip(),
            "subject": parts[1].strip() if len(parts) > 1 else "",
            "date": day,
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


def _make_step(index: int, commits: list[dict[str, str]], start: str,
               vllm_lines: int, effective_lines: int,
               needs_adaptation: bool,
               impacts: dict[str, dict | None] | None = None) -> dict[str, Any]:
    tags: set[str] = set()
    if impacts:
        for c in commits:
            impact = impacts.get(c["sha"])
            if impact:
                tags.update(impact.get("tags", []) or [])
    # Commits vllm-report routed to ascend work (analyzed+affected, or
    # unanalyzed → conservative).  The PR description cites THESE as the
    # upstream drivers of a step's adaptation — a step's end_commit is
    # often a trailing no-op/doc commit that had nothing to do with the
    # change the adapter actually made (PR #16424 shipped rows citing
    # ROCm/doc-only commits with another commit's adaptation text).
    affected_commits = [
        c["sha"] for c in commits
        if (impacts is None or impacts.get(c["sha"]) is None
            or impacts[c["sha"]].get("ascend_affected", False))
    ]
    return {
        "index": index,
        "id": f"step-{index}",
        "commits": list(commits),
        "commit_count": len(commits),
        "start_commit": start,
        "end_commit": commits[-1]["sha"],
        "affected_commits": affected_commits,
        "vllm_changed_lines": vllm_lines,
        "effective_lines": effective_lines,
        "line_budget": LINE_BUDGET,
        "commit_count_budget": _commit_count_budget(),
        "needs_adaptation": needs_adaptation,
        "tags": sorted(tags),
    }


def _day_buckets(commits: list[dict[str, str]]) -> list[list[dict[str, str]]]:
    """Group a chronological commit list into consecutive calendar-day
    buckets (commits arrive oldest-first from git log --reverse)."""
    buckets: list[list[dict[str, str]]] = []
    for commit in commits:
        if buckets and buckets[-1][-1].get("date") == commit.get("date"):
            buckets[-1].append(commit)
        else:
            buckets.append([commit])
    return buckets


def _split_evenly(day_commits: list[dict[str, str]],
                  effective: list[int], k: int) -> list[list[dict[str, str]]]:
    """Split one day's commits into at most k steps with an even
    effective-line share.

    Boundaries fall after the commit where the cumulative effective lines
    first reach j*total/k.  A huge commit naturally lands alone (the
    boundary lands right after it); zero-effective tails merge into the
    last bucket rather than forming empty steps.  When the day has fewer
    commits than k, each commit becomes its own step instead of empty
    buckets."""
    n = len(day_commits)
    if n <= k:
        return [[c] for c in day_commits]
    total = sum(effective)
    prefix = list(itertools.accumulate(effective))
    boundaries: list[int] = []
    j = 1
    for i in range(n - 1):
        while (j < k and prefix[i] >= j * total / k
               and (not boundaries or i + 1 > boundaries[-1])):
            boundaries.append(i + 1)
            j += 1
    buckets: list[list[dict[str, str]]] = []
    prev = 0
    for b in boundaries:
        buckets.append(day_commits[prev:b])
        prev = b
    buckets.append(day_commits[prev:])
    return buckets


def _plan_steps(
    commits: list[dict[str, str]],
    lines_per_commit: dict[str, int],
    impacts: dict[str, dict | None],
    base_commit: str,
) -> list[dict[str, Any]]:
    """Day-bounded grouping: at most MAX_STEPS_PER_DAY steps per calendar
    day (user decision 2026-10-14).

    Every commit goes into a step (so verified.commit advances across the
    full range).  A commit's *effective* lines are:
      - 0 if vllm-report analyzed it and marked ascend_affected=False
        (the commit's own changes don't need ascend work, but plan still
        walks through it to keep ordering intact)
      - the full vllm/ line count otherwise (analyzed + affected, or
        unanalyzed where we fall back to conservative line-count)

    Within a day, the LINE_BUDGET / COMMIT_BUDGET pair only decides
    whether the day is one step or is split into MAX_STEPS_PER_DAY steps
    by even effective-line share — budget accumulation never produces
    more than MAX_STEPS_PER_DAY steps for a day.  The cost of the cap is
    coarser failure bisection; the win is that each day costs at most
    MAX_STEPS_PER_DAY e2e+UT validation rounds.

    The adapter runs on every step.  If the adapter's analysis confirms
    the step needs no ascend change (all commits ascend_affected=False AND
    no baseline issue surfaces), the adapter sets is_noop=True on the
    result and the flow skips e2e for that step.
    """
    placed_shas: set[str] = set()
    steps: list[dict[str, Any]] = []
    start = base_commit

    def close_step(bucket: list[dict[str, str]],
                   vllm_lines: int, effective_lines: int) -> None:
        nonlocal start
        needs_adapt = _step_needs_adaptation(bucket, impacts)
        steps.append(_make_step(len(steps) + 1, bucket, start,
                                vllm_lines, effective_lines,
                                needs_adapt, impacts))
        for c in bucket:
            placed_shas.add(c["sha"])
        start = bucket[-1]["sha"]

    for day_commits in _day_buckets(commits):
        effective = [
            _effective_lines(impacts.get(c["sha"]), lines_per_commit.get(c["sha"], 0))
            for c in day_commits
        ]
        total_eff = sum(effective)
        # A zero-effective day is a no-op workload (vllm-report routed every
        # commit to ascend_affected=False) — the adapter sets is_noop and
        # skips e2e, so commit count is irrelevant; keep it one step.
        fits_one_step = (total_eff <= LINE_BUDGET
                         and (len(day_commits) <= _commit_count_budget()
                              or total_eff == 0))
        if fits_one_step or len(day_commits) <= 1 or MAX_STEPS_PER_DAY <= 1:
            close_step(day_commits, sum(lines_per_commit.get(c["sha"], 0)
                                        for c in day_commits), total_eff)
            continue
        buckets = _split_evenly(day_commits, effective, MAX_STEPS_PER_DAY)
        for bucket in buckets:
            close_step(bucket,
                       sum(lines_per_commit.get(c["sha"], 0) for c in bucket),
                       sum(_effective_lines(impacts.get(c["sha"]),
                                            lines_per_commit.get(c["sha"], 0))
                           for c in bucket))

    # If commits existed but produced zero steps (shouldn't happen), fall
    # back to a single step covering the full range.
    if not steps and commits:
        steps.append(_make_step(1, commits, base_commit, 0, 0,
                                _step_needs_adaptation(commits, impacts), impacts))
        for c in commits:
            placed_shas.add(c["sha"])

    # Safety net: every input commit must appear in exactly one step.
    missing = [c["sha"] for c in commits if c["sha"] not in placed_shas]
    if missing:
        ts_print(f"[plan] WARNING: {len(missing)} commits not placed in any "
                 f"step, creating fallback step: {[s[:8] for s in missing[:5]]}")
        steps.append(_make_step(len(steps) + 1,
                                [c for c in commits if c["sha"] in missing],
                                steps[-1]["end_commit"] if steps else base_commit,
                                0, 0, True, impacts))
        for c in steps[-1]["commits"]:
            placed_shas.add(c["sha"])

    return steps


def emit_plan_summary(plan: dict[str, Any]) -> None:
    """Surface the step plan where the Actions UI actually shows it.

    The raw log line ([analyze] planned N step(s) ...) is buried in the
    main2main step's tens of thousands of lines, so also emit a ::notice
    workflow command (renders as an annotation at the top of the job
    page) and a markdown table appended to $GITHUB_STEP_SUMMARY (renders
    in the job's Summary view).  Outside Actions — local runs, tests —
    GITHUB_STEP_SUMMARY is unset and only the notice line is printed.
    """
    steps = plan["steps"]
    total = len(steps)
    ts_print(f"::notice::planned {total} step(s) covering "
             f"{plan['total_commits']} commit(s)")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary_path:
        return
    lines = [
        "## Main2Main step plan",
        f"{total} step(s) covering {plan['total_commits']} commit(s)",
        "",
        "| # | commits | date (UTC) | effective lines | upstream range |",
        "|---|---------|------------|-----------------|----------------|",
    ]
    for step in steps:
        dates = sorted({c.get("date", "") for c in step["commits"]})
        span = dates[0] if len(dates) == 1 else f"{dates[0]}..{dates[-1]}"
        lines.append(
            f"| {step['index']} | {step['commit_count']} | {span} | "
            f"{step['effective_lines']} | "
            f"`{step['start_commit'][:8]}..{step['end_commit'][:8]}` |")
    with open(summary_path, "a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


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
    unaffected = sum(1 for v in impacts.values() if v and not v.get("ascend_affected", False))
    ts_print(f"[plan] vllm-report impact: {analyzed}/{len(commits)} analyzed, "
             f"{unaffected} marked ascend_affected=False (0 effective lines), "
             f"{len(commits) - analyzed} unanalyzed (fallback to vllm-line-budget)")

    steps = _plan_steps(commits, lines_per_commit, impacts, base_commit)
    _enrich_steps_with_diff(vllm_path, steps)
    return {
        "base_commit": base_commit,
        "target_commit": target_commit,
        "total_commits": sum(s["commit_count"] for s in steps),
        "steps": steps,
    }