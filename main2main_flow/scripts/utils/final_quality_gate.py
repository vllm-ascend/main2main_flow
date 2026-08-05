"""Final quality gate: format + mypy + UT check before push.

Runs once after all steps complete (in generate_final_post, before
push_to_github).  This is the main2main equivalent of CI's pre-commit
"Run mypy" + "Run pre-commit" + "Run selected tests without device" steps -
by running them here on the final cumulative diff, we catch format/mypy/UT
issues in the exact environment CI will use, and give the adapter a chance
to fix them before push.

Usage:
    passed, error_logs = run_final_quality_gate(
        ascend_path, vllm_path, release_tag, log_dir,
    )
    if not passed:
        # error_logs contains quality_gate.json path for adapter-fix
"""
from __future__ import annotations

import json
from pathlib import Path

from main2main_flow.scripts.utils.pre_ci_check import (
    _check_format,
    _check_mypy,
    _check_ut,
)
from main2main_flow.scripts.utils.utils import ts_print


def run_final_quality_gate(
    ascend_path: str | Path,
    vllm_path: str | Path,
    release_tag: str,
    log_dir: Path,
) -> tuple[bool, list[str]]:
    """Run format + mypy + UT on the final cumulative diff, before push.

    Returns (passed, error_logs).  error_logs is empty when passed;
    otherwise contains a single path to quality_gate.json (which holds
    the full violation list for adapter-fix to read).
    """
    repo = Path(ascend_path)
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    gate_path = log_dir / "quality_gate.json"

    ts_print("\n[final_quality_gate] running format + mypy + UT on final diff...")

    fmt = _check_format(repo)
    mypy = _check_mypy(repo, vllm_path)
    ut = _check_ut(repo, vllm_path)

    fmt_ok = len(fmt["violations"]) == 0 or fmt.get("skipped", False)
    mypy_ok = len(mypy["violations"]) == 0 or mypy.get("skipped", False)
    ut_ok = len(ut["violations"]) == 0 or ut.get("skipped", False)
    all_passed = fmt_ok and mypy_ok and ut_ok

    checks: list[dict] = []
    checks.append({
        "name": "format",
        "passed": fmt_ok,
        "detail": fmt.get("detail", ""),
        "violations": fmt.get("violations", []),
        "skipped": fmt.get("skipped", False),
    })
    checks.append({
        "name": "mypy",
        "passed": mypy_ok,
        "detail": mypy.get("detail", ""),
        "violations": mypy.get("violations", []),
        "skipped": mypy.get("skipped", False),
    })
    checks.append({
        "name": "ut",
        "passed": ut_ok,
        "detail": ut.get("detail", ""),
        "violations": ut.get("violations", []),
        "skipped": ut.get("skipped", False),
    })

    result = {"all_passed": all_passed, "checks": checks}
    gate_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")

    if all_passed:
        ts_print("\n[final_quality_gate] PASSED (format + mypy + UT clean)")
        return True, []
    ts_print(f"\n[final_quality_gate] FAILED -> {gate_path}")
    for c in checks:
        if not c["passed"]:
            ts_print(f"  {c['name']}: {c['detail']}")
    return False, [str(gate_path)]
