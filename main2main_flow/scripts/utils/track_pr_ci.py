#!/usr/bin/env python3
"""Track main2main adaptation PRs and record their CI results.

Daily job: identify PRs created by main2main (title pattern "adapt to
vLLM main"), fetch their CI check results, and write structured records
to vllm-report's data directory.  Failed checks get a failure excerpt
(deepest exception) extracted so the lesson-generation step (phase 2)
has material to work with.

Output: data/vllm-ascend/pr_ci_results/<date>.json
  {
    "date": "2026-08-24",
    "prs": [
      {
        "pr_number": 14778,
        "title": "...",
        "vllm_commit": "d29dc3ab",
        "state": "open",
        "created_at": "...",
        "ci_conclusion": "failure",
        "ci_class": "release-only",
        "failing_lanes": ["release"],
        "release_lane_failed": true,
        "main_lane_failed": false,
        "labels": ["main2main"],
        "checks": [
          {
            "name": "run-selected-tests (vllm@v0.28.0) / cpu-0 card-(part 1-1)",
            "lane": "release",
            "conclusion": "failure",
            "details_url": "...",
            "failure_summary": "TypeError: __init__() missing 1 required positional argument: 'max_seq_len_np' ..."
          }
        ]
      }
    ]
  }

PR CI runs TWO vllm lanes (pr_test.yaml): a pinned main commit and the
frozen release tag.  Each check is classified into its lane and the record
carries which lane(s) failed (``ci_class``: release-only / main / both /
none) — a release-only failure usually means a pinned-release adaptation
gap, not a bad main-lane adaptation.  A PR with no checks at all and
mergeStateStatus CONFLICTING never ran e2e; it is recorded as
``blocked-conflicting`` / ``no-e2e-ran`` instead of the signal-less
``unknown``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from main2main_flow.scripts.utils.utils import ts_print

REPO = "vllm-project/vllm-ascend"
PR_TITLE_RE = re.compile(r"adapt to vLLM main\s*\(([0-9a-f]{7,})\)", re.IGNORECASE)
FAIL_LOG_MAX_CHARS = 8000
FAIL_LOG_SCAN_LIMIT = 512_000  # scan up to 512KB of the log for error patterns
_ERROR_RE = re.compile(
    r"((?:[A-Za-z_][\w]*\.)*[A-Za-z_][\w]*(?:Error|Exception)):\s*(.+)"
)


def _gh_json(args: list[str], fields: str = "") -> dict | list:
    cmd = ["gh", *args, "--repo", REPO]
    if fields:
        cmd += ["--json", fields]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return []
    try:
        return json.loads(r.stdout)
    except (json.JSONDecodeError, ValueError):
        return []


def _gh_pr_list(since: str) -> list[dict]:
    prs = _gh_json([
        "pr", "list", "--search", 'adapt to vLLM main in:title',
        "--state", "all", "--limit", "30",
    ], fields="number,title,createdAt,state,url,labels")
    if not isinstance(prs, list):
        return []
    cutoff = datetime.fromisoformat(since.replace("Z", "+00:00"))
    result = []
    for pr in prs:
        created = pr.get("createdAt", "")
        if not created:
            continue
        try:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dt < cutoff:
            continue
        m = PR_TITLE_RE.search(pr.get("title", ""))
        if not m:
            continue
        result.append({**pr, "vllm_commit": m.group(1)})
    return result


_LANE_RE = re.compile(r"vllm@([^)]+)\)")
_MAIN_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_RELEASE_VER_RE = re.compile(r"^v?\d+\.\d+")
_LEGACY_RELEASE_RE = re.compile(r"^run-selected-tests \((v?\d+\.\d+)")


def _lane_from_check_name(name: str) -> str:
    """Classify a PR CI check name into its vllm lane.

    pr_test.yaml names look like
    ``run-selected-tests (vllm@<vllm_version>) / ...`` where vllm_version
    is the 40-hex main pin or a release tag (v0.28.0).  Legacy names
    (``run-selected-tests (v0.27.1) / ...``) predate the dual-lane split
    and only ever ran the release pin."""
    m = _LANE_RE.search(name)
    if m:
        ver = m.group(1).strip()
        if _MAIN_SHA_RE.match(ver):
            return "main"
        if _RELEASE_VER_RE.match(ver):
            return "release"
        return "unknown"
    if _LEGACY_RELEASE_RE.match(name):
        return "release"
    return "unknown"


def _ci_class(failing_lanes: set[str]) -> str:
    """Classify which vllm lane(s) failed.  Checks with unparseable names
    carry lane "unknown" — alongside a known lane they collapse into it;
    alone they stay "unknown" (never misreport as main/release)."""
    if not failing_lanes:
        return "none"
    if "main" in failing_lanes and "release" in failing_lanes:
        return "both"
    if "release" in failing_lanes:
        return "release-only"
    if "main" in failing_lanes:
        return "main"
    return "unknown"


def _extract_failure_summary(job_id: str) -> str | None:
    try:
        r = subprocess.run(
            ["gh", "api", f"repos/{REPO}/actions/jobs/{job_id}/logs"],
            capture_output=True, text=True, timeout=90,
        )
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    text = r.stdout
    # Search the full log (up to scan limit) — errors are often in the
    # middle (pytest output), not at the tail (teardown).
    if len(text) > FAIL_LOG_SCAN_LIMIT:
        text = text[:FAIL_LOG_SCAN_LIMIT]
    matches = list(_ERROR_RE.finditer(text))
    if not matches:
        return None
    seen: set[str] = set()
    summaries: list[str] = []
    for m in matches:
        msg = m.group(2).strip()[:200]
        exc = f"{m.group(1)}: {msg}"
        if exc not in seen:
            seen.add(exc)
            summaries.append(exc)
        if len(summaries) >= 5:
            break
    return " | ".join(summaries) if summaries else None


def _get_checks(pr_number: int, skip_log_fetch: bool = False) -> list[dict]:
    checks = _gh_json(["pr", "checks", str(pr_number)],
                      fields="name,state,bucket,link")
    if not isinstance(checks, list):
        return []
    result = []
    for c in checks:
        state = (c.get("state", "") or "").upper()
        name = c.get("name", "")
        bucket = c.get("bucket", "")
        if not name or bucket == "skipping" or state == "SKIPPED":
            continue
        entry = {
            "name": name,
            "lane": _lane_from_check_name(name),
            "conclusion": state.lower(),
            "details_url": c.get("link", ""),
        }
        if (bucket == "fail" or state in ("FAILURE", "CANCEL", "CANCELLED")) and not skip_log_fetch:
            url = entry["details_url"] or ""
            job_id = url.rstrip("/").split("/")[-1] if "/job/" in url else ""
            if job_id.isdigit():
                summary = _extract_failure_summary(job_id)
                if summary:
                    entry["failure_summary"] = summary
        result.append(entry)
    return result


def _load_processed_prs(data_dir: Path) -> dict[int, dict]:
    """Return {pr_number: {"conclusion": ..., "ci_class": ...}} for PRs
    already recorded with failure summaries in previous runs.  Used to
    skip re-fetching CI logs for PRs whose results haven't changed — a
    changed conclusion OR a changed lane classification (e.g. records
    written before lane tagging existed) forces a re-fetch."""
    processed: dict[int, dict] = {}
    results_dir = data_dir / "vllm-ascend" / "pr_ci_results"
    if not results_dir.exists():
        return processed
    for f in sorted(results_dir.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for pr in data.get("prs", []):
            num = pr.get("pr_number")
            if num and num not in processed:
                # Only mark as processed if the PR had failure_summary
                # extracted (i.e., the expensive log fetch already done)
                has_summary = any(
                    c.get("failure_summary") for c in pr.get("checks", []))
                if has_summary or pr.get("ci_conclusion") == "success":
                    processed[num] = {
                        "conclusion": pr.get("ci_conclusion", ""),
                        "ci_class": pr.get("ci_class", ""),
                    }
    return processed


def _load_prev_record(data_dir: Path, pr_number: int) -> dict | None:
    """Load the most recent record for a PR from previous result files."""
    results_dir = data_dir / "vllm-ascend" / "pr_ci_results"
    if not results_dir.exists():
        return None
    for f in sorted(results_dir.glob("*.json"), reverse=True):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for pr in data.get("prs", []):
            if pr.get("pr_number") == pr_number:
                return pr
    return None


def track(data_dir: Path, days: int = 7) -> dict:
    since = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)).isoformat() + "Z"
    ts_print(f"[track_pr_ci] searching main2main PRs since {since}")
    prs = _gh_pr_list(since)
    ts_print(f"[track_pr_ci] found {len(prs)} main2main PRs")

    # Dedup: skip failure_summary extraction for PRs already processed
    # (failure summaries extracted or CI passed) in previous runs.
    processed = _load_processed_prs(data_dir)
    if processed:
        ts_print(f"[track_pr_ci] {len(processed)} PRs already processed "
                 f"(will skip log fetch for these unless CI result changed)")

    records = []
    for pr in prs:
        pr_number = pr["number"]
        prev_info = processed.get(pr_number)
        ts_print(f"[track_pr_ci] PR #{pr_number}: {pr.get('title','')[:60]}")

        # If the PR was previously processed and CI conclusion AND lane
        # classification are unchanged, reuse the previous record (skip the
        # expensive log fetch).
        if prev_info is not None:
            checks = _get_checks(pr_number, skip_log_fetch=True)
            failing = [c for c in checks if c["conclusion"] in ("failure", "cancelled")]
            passing = [c for c in checks if c["conclusion"] == "success"]
            new_conclusion = "failure" if failing else ("success" if passing else "unknown")
            new_ci_class = _ci_class({c.get("lane", "unknown") for c in failing})
            if new_conclusion == prev_info["conclusion"] and \
                    new_ci_class == prev_info["ci_class"]:
                ts_print(f"[track_pr_ci] PR #{pr_number}: already processed "
                         f"({prev_info['conclusion']}, {prev_info['ci_class']}), "
                         f"skipping log fetch")
                # Load previous record to preserve failure_summary
                prev_record = _load_prev_record(data_dir, pr_number)
                if prev_record:
                    prev_record["created_at"] = pr.get("createdAt", prev_record.get("created_at", ""))
                    prev_record["state"] = pr.get("state", prev_record.get("state", ""))
                    prev_record["already_analyzed"] = True
                    records.append(prev_record)
                    continue
            else:
                ts_print(f"[track_pr_ci] PR #{pr_number}: CI result changed "
                         f"({prev_info['conclusion']}/{prev_info['ci_class']} -> "
                         f"{new_conclusion}/{new_ci_class}) — re-fetching logs")
                # checks so far came from skip_log_fetch=True: no
                # failure_summary.  A record built from them would carry the
                # new conclusion without the logs to explain it.
                checks = _get_checks(pr_number)
        else:
            checks = _get_checks(pr_number)

        failing = [c for c in checks if c["conclusion"] in ("failure", "cancelled")]
        passing = [c for c in checks if c["conclusion"] == "success"]
        failing_lanes = sorted({c.get("lane", "unknown") for c in failing})
        # A PR with zero checks may be a merge conflict: no merge ref, the
        # pull_request e2e never ran — report that instead of "unknown".
        merge_conflicting = False
        if not checks:
            info = _gh_json(["pr", "view", str(pr_number)],
                            fields="mergeable,mergeStateStatus")
            if (isinstance(info, dict) and (info.get("mergeStateStatus")
                                            or "").upper() == "CONFLICTING"):
                merge_conflicting = True
        if merge_conflicting:
            ci_conclusion = "blocked-conflicting"
            ci_class = "no-e2e-ran"
        else:
            ci_conclusion = "failure" if failing else ("success" if passing else "unknown")
            ci_class = _ci_class(set(failing_lanes))
        record = {
            "pr_number": pr_number,
            "pr_url": pr.get("url", ""),
            "title": pr.get("title", ""),
            "vllm_commit": pr.get("vllm_commit", ""),
            "state": pr.get("state", ""),
            "created_at": pr.get("createdAt", ""),
            "labels": pr.get("labels", []),
            "ci_conclusion": ci_conclusion,
            "ci_class": ci_class,
            "failing_lanes": failing_lanes,
            "release_lane_failed": "release" in failing_lanes,
            "main_lane_failed": "main" in failing_lanes,
            "total_checks": len(checks),
            "passing_checks": len(passing),
            "failing_checks": len(failing),
            "checks": checks,
        }
        records.append(record)

    date_str = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%d")
    result = {"date": date_str, "prs": records}
    out_dir = data_dir / "vllm-ascend" / "pr_ci_results"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date_str}.json"
    out_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    ts_print(f"[track_pr_ci] wrote {len(records)} PR records to {out_path}")
    return result


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(description="Track main2main PR CI results")
    p.add_argument("--data-dir", default=os.environ.get("DATA_DIR", ""),
                   help="vllm-report data directory")
    p.add_argument("--days", type=int, default=7,
                   help="look back N days for PRs")
    args = p.parse_args()
    if not args.data_dir:
        ts_print("[track_pr_ci] ERROR: --data-dir required (or set DATA_DIR env)")
        sys.exit(1)
    track(Path(args.data_dir), days=args.days)


if __name__ == "__main__":
    main()
