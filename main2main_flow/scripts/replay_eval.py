#!/usr/bin/env python3
"""Replay evaluation harness for the main2main pipeline (scaffold).

Replays historical upstream ranges through the flow and scores the produced
adaptation against a known-good expectation (e.g. the merged main2main PR for
that range). Run the same cases before switching models or prompts to get a
precision/recall scorecard instead of intuition.

Usage:
    python -m main2main_flow.scripts.replay_eval \\
        --vllm-path <path> --ascend-path <path> \\
        --cases cases.json [--workdir <dir>]

cases.json — a JSON list of objects:
    {
      "name": "pr-1234",
      "base_commit": "<40-hex>",
      "target_commit": "<40-hex>",
      "expected_files": ["vllm_ascend/...", ...],
      "expected_patch": "merged.patch"      # optional; file-set is unioned in
    }

Notes:
  - The caller prepares the vllm-ascend checkout so that its pinned base
    commit matches the case's base_commit; detect.json is cross-checked and
    a mismatch is recorded per case as "base_mismatch".
  - The caller controls MAIN2MAIN_MODEL (required for real agent replays).
    SKIP_AI_ANALYSIS=true gives a plumbing-only replay (no adaptation is
    produced — useful to validate the harness wiring itself).
  - SKIP_E2E_TEST is forced to "true": replays score the produced patch,
    they never run hardware tests.
  - Cases share the flow workspace (wiped per kickoff); each case's
    final_target.patch is copied to <workdir>/<name>/ before the next runs.

Output: <workdir>/replay_report.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path
from typing import Any

from main2main_flow.utils import (
    DETECT_FILE,
    FINAL_TARGET_PATCH_FILE,
    WORKSPACE_DIR,
    ts_print,
)


def _patch_file_set(patch_text: str) -> set[str]:
    """Extract the touched-file set from a unified git patch."""
    files: set[str] = set()
    for line in patch_text.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4 and parts[3].startswith("b/"):
                files.add(parts[3][2:])
    return files


def _expected_file_set(case: dict[str, Any], cases_dir: Path) -> set[str]:
    expected = set(case.get("expected_files") or [])
    patch_ref = case.get("expected_patch")
    if patch_ref:
        p = Path(patch_ref)
        if not p.is_absolute():
            p = cases_dir / p
        if p.exists():
            expected |= _patch_file_set(p.read_text(encoding="utf-8"))
        else:
            ts_print(f"[replay] Warning: expected_patch not found: {p}")
    return expected


def _score(produced: set[str], expected: set[str]) -> dict[str, Any]:
    tp = len(produced & expected)
    return {
        "true_positives": tp,
        "precision": round(tp / len(produced), 3) if produced else 0.0,
        "recall": round(tp / len(expected), 3) if expected else 0.0,
        "missing_files": sorted(expected - produced),
        "unexpected_files": sorted(produced - expected),
    }


def _run_case(case: dict[str, Any], vllm_path: Path, ascend_path: Path,
              case_dir: Path) -> tuple[dict[str, Any], set[str]]:
    from main2main_flow.flow import Main2MainFlow  # deferred: pulls in crewai

    os.environ["VLLM_TARGET_COMMIT"] = case["target_commit"]
    flow = Main2MainFlow()
    flow.kickoff(inputs={
        "vllm_path": str(vllm_path),
        "vllm_ascend_path": str(ascend_path),
    })

    record: dict[str, Any] = {}
    detect_path = WORKSPACE_DIR / DETECT_FILE
    if detect_path.exists():
        detected = json.loads(detect_path.read_text(encoding="utf-8"))
        record["detected_base"] = detected.get("base_commit", "")
        record["base_mismatch"] = (
            case.get("base_commit", "") not in ("", record["detected_base"])
        )
    status_path = WORKSPACE_DIR / "final_status.json"
    if status_path.exists():
        status = json.loads(status_path.read_text(encoding="utf-8"))
        record["status"] = status.get("status", "")

    produced: set[str] = set()
    patch_path = WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE
    if patch_path.exists():
        produced = _patch_file_set(patch_path.read_text(encoding="utf-8"))
        shutil.copy2(patch_path, case_dir / FINAL_TARGET_PATCH_FILE)
    record["produced_files"] = sorted(produced)
    return record, produced


def run_replay(vllm_path: Path, ascend_path: Path, cases_path: Path,
               workdir: Path) -> dict[str, Any]:
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise RuntimeError(f"{cases_path} must contain a JSON list of cases")
    workdir.mkdir(parents=True, exist_ok=True)
    os.environ["SKIP_E2E_TEST"] = "true"  # replays never run hardware tests

    results: list[dict[str, Any]] = []
    for i, case in enumerate(cases):
        name = str(case.get("name") or f"case-{i + 1}")
        case_dir = workdir / name
        case_dir.mkdir(parents=True, exist_ok=True)
        ts_print(f"[replay] === {name}: {case.get('base_commit', '?')[:8]}"
                 f"..{case.get('target_commit', '?')[:8]} ===")
        try:
            record, produced = _run_case(case, vllm_path, ascend_path, case_dir)
        except Exception as exc:  # keep scoring the remaining cases
            ts_print(f"[replay] {name} crashed: {exc!r}")
            results.append({"name": name, "error": repr(exc)})
            continue
        record["name"] = name
        expected = _expected_file_set(case, cases_path.parent)
        record["expected_files"] = sorted(expected)
        record.update(_score(produced, expected))
        results.append(record)

    scored = [r for r in results if "precision" in r]
    report = {
        "total_cases": len(results),
        "scored_cases": len(scored),
        "mean_precision": round(sum(r["precision"] for r in scored) / len(scored), 3) if scored else 0.0,
        "mean_recall": round(sum(r["recall"] for r in scored) / len(scored), 3) if scored else 0.0,
        "cases": results,
    }
    report_path = workdir / "replay_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n",
                           encoding="utf-8")
    ts_print(f"[replay] Report written to {report_path}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Replay historical main2main ranges and score the produced patches."
    )
    parser.add_argument("--vllm-path", type=Path, required=True,
                        help="Local vllm repository path.")
    parser.add_argument("--ascend-path", type=Path, required=True,
                        help="Local vllm-ascend repository path (checked out at the case's base).")
    parser.add_argument("--cases", type=Path, required=True,
                        help="JSON list of replay cases.")
    parser.add_argument("--workdir", type=Path,
                        default=Path(__file__).parent.parent.parent / "replay_workdir",
                        help="Directory for per-case artifacts and replay_report.json.")
    args = parser.parse_args()

    report = run_replay(args.vllm_path, args.ascend_path, args.cases, args.workdir)
    ts_print(f"[replay] {report['scored_cases']}/{report['total_cases']} cases scored, "
             f"mean precision={report['mean_precision']} recall={report['mean_recall']}")


if __name__ == "__main__":
    main()
