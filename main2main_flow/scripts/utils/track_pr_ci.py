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
        "labels": ["ready-all"],
        "checks": [
          {
            "name": "run-selected-tests (v0.27.1) / cpu-0 card-(part 1-1)",
            "conclusion": "failure",
            "details_url": "...",
            "failure_summary": "ImportError: cannot import name 'cp_local_slot' ..."
          }
        ]
      }
    ]
  }
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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


def _load_processed_prs(data_dir: Path) -> dict[int, str]:
    """Return {pr_number: ci_conclusion} for PRs already recorded with
    failure summaries in previous runs.  Used to skip re-fetching CI
    logs for PRs whose results haven't changed."""
    processed: dict[int, str] = {}
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
                    processed[num] = pr.get("ci_conclusion", "")
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
        prev_conclusion = processed.get(pr_number)
        ts_print(f"[track_pr_ci] PR #{pr_number}: {pr.get('title','')[:60]}")

        # If the PR was previously processed and CI conclusion is unchanged,
        # reuse the previous record (skip the expensive log fetch).
        if prev_conclusion is not None:
            checks = _get_checks(pr_number, skip_log_fetch=True)
            failing = [c for c in checks if c["conclusion"] in ("failure", "cancelled")]
            passing = [c for c in checks if c["conclusion"] == "success"]
            new_conclusion = "failure" if failing else ("success" if passing else "unknown")
            if new_conclusion == prev_conclusion:
                ts_print(f"[track_pr_ci] PR #{pr_number}: already processed "
                         f"({prev_conclusion}), skipping log fetch")
                # Load previous record to preserve failure_summary
                prev_record = _load_prev_record(data_dir, pr_number)
                if prev_record:
                    prev_record["created_at"] = pr.get("createdAt", prev_record.get("created_at", ""))
                    prev_record["state"] = pr.get("state", prev_record.get("state", ""))
                    prev_record["already_analyzed"] = True
                    records.append(prev_record)
                    continue
        else:
            checks = _get_checks(pr_number)

        failing = [c for c in checks if c["conclusion"] in ("failure", "cancelled")]
        passing = [c for c in checks if c["conclusion"] == "success"]
        record = {
            "pr_number": pr_number,
            "pr_url": pr.get("url", ""),
            "title": pr.get("title", ""),
            "vllm_commit": pr.get("vllm_commit", ""),
            "state": pr.get("state", ""),
            "created_at": pr.get("createdAt", ""),
            "labels": pr.get("labels", []),
            "ci_conclusion": "failure" if failing else ("success" if passing else "unknown"),
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


def ts_print(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


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
