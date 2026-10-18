"""Final quality gate: format + mypy + UT check before push.

Runs once after all steps complete (in generate_final_post, before
push_to_github).  This is the main2main equivalent of CI's pre-commit
"Run mypy" + "Run pre-commit" + CPU-UT steps - by running them here on
the final cumulative diff, we catch format/mypy/UT issues in the exact
environment CI will use, and give the adapter a chance to fix them
before push.

UT (_check_ut) runs the CPU-routed tests/ut/* files with FULL isolation:
each file in its own fresh subprocess (mock pollution immune) + a fake
npu-smi on the PATH so conftest.py takes the mock path even on the A2
NPU runner.  See pre_ci_check._check_ut docstring for details.

Usage:
    passed, error_logs = run_final_quality_gate(
        ascend_path, vllm_path, log_dir,
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
from main2main_flow.scripts.utils.utils import exclude_generated_artifacts, ts_print

# The only tests/examples failures that count as gate violations.  CI's
# tools/mypy.sh type-checks the whole tests tree; this runner also reports
# ~336 mock-pattern [call-arg]/[union-attr] errors that the lint image
# does not, so everything except "a vllm module no longer resolves" is
# noise at the gate.
_VLLM_MISSING_MODULE_SIGS = (
    'Cannot find implementation or library stub for module named "vllm.',
)


def run_final_quality_gate(
    ascend_path: str | Path,
    vllm_path: str | Path,
    log_dir: Path,
    release_tag: str = "",
    vllm_release_path: str | Path | None = None,
) -> tuple[bool, list[str]]:
    """Run format + mypy on the final cumulative diff, before push.

    UT gate runs the CPU-UT batch against the target main vllm checkout.
    When ``vllm_release_path`` (a worktree of the pinned release tag) is
    given, mypy runs a second pass against the release tree — the same
    dual-lane validation as per-step pre_ci.  mypy runs with a fresh
    (non-incremental) cache, and a second gate-only pass resolves the
    full tests/examples tree against vllm, mirroring CI's whole-tree
    tools/mypy.sh.

    Returns (passed, error_logs).  error_logs is empty when passed;
    otherwise contains a single path to quality_gate.json (which holds
    the full violation list for adapter-fix to read).
    """
    repo = Path(ascend_path)
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    gate_path = log_dir / "quality_gate.json"

    # Isolate non-business generated artifacts (e.g. torch_compile_debug/
    # from the previous round's a2 UT run) before any check — they must
    # never fail format or leak into the PR diff.
    n_unstaged = exclude_generated_artifacts(repo)
    if n_unstaged:
        ts_print(f"[final_quality_gate] unstaged {n_unstaged} generated "
                 f"artifact file(s) (excluded from checks)")

    # UT gate on/off switch.  Verified 166/166 clean on A2, but keep a
    # kill-switch so a UT-environment regression can't block push — set
    # MAIN2MAIN_UT_GATE=0 to skip _check_ut entirely (format + mypy only).
    import os as _os
    ut_enabled = _os.environ.get("MAIN2MAIN_UT_GATE", "1").lower() not in (
        "0", "false", "no", "off")

    ts_print("\n[final_quality_gate] running format + mypy"
             + (" + UT" if ut_enabled else " (UT DISABLED)") + " on final diff...")

    fmt = _check_format(repo)
    # fresh_cache: the gate certifies the final diff.  An incremental
    # cache entry written by an earlier round whose mypy.ini edit was
    # later discarded must never decide the verdict (PR 19089: a
    # [mypy-vllm...glm5_next] ignore_missing_imports override from a dead
    # branch whitewashed the next run's gate while CI, with a clean cache,
    # failed on the same import).
    mypy = _check_mypy(repo, vllm_path, vllm_release_path, fresh_cache=True)
    # CI's tools/mypy.sh checks the WHOLE tests/examples tree; pre_ci only
    # checks vllm_ascend + *changed* test files, so an unchanged test that
    # imports a module the target vllm deleted is invisible to every step
    # (PR 19089: tests/ut/models/test_glm5_next_mtp.py still imported the
    # GLM5-Next config that vllm#57387 moved into transformers, and CI's
    # pre-commit rejected the PR).  One pass is enough: import resolution
    # does not vary across 3.10/3.11/3.12 for these module-level imports.
    test_imports = _check_mypy(
        repo, vllm_path,
        fresh_cache=True,
        targets=("examples", "tests"),
        filter_sigs=_VLLM_MISSING_MODULE_SIGS,
        python_versions=("3.10",),
    )
    if ut_enabled:
        ut = _check_ut(repo, vllm_path,
                       vllm_release_path=vllm_release_path,
                       release_tag=release_tag)
    else:
        ut = {"violations": [], "detail": "UT gate disabled (MAIN2MAIN_UT_GATE=0)",
              "skipped": True}
        ts_print("[final_quality_gate] UT gate DISABLED via "
                 "MAIN2MAIN_UT_GATE=0 — skipping _check_ut")

    fmt_ok = len(fmt["violations"]) == 0 or fmt.get("skipped", False)
    mypy_ok = len(mypy["violations"]) == 0 or mypy.get("skipped", False)
    imports_ok = (len(test_imports["violations"]) == 0
                  or test_imports.get("skipped", False))
    ut_ok = len(ut["violations"]) == 0 or ut.get("skipped", False)
    all_passed = fmt_ok and mypy_ok and imports_ok and ut_ok

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
        "name": "mypy-tests-imports",
        "passed": imports_ok,
        "detail": test_imports.get("detail", ""),
        "violations": test_imports.get("violations", []),
        "skipped": test_imports.get("skipped", False),
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
        ts_print("\n[final_quality_gate] PASSED (format + mypy + "
                 "tests imports + UT clean)")
        return True, []
    ts_print(f"\n[final_quality_gate] FAILED -> {gate_path}")
    for c in checks:
        if not c["passed"]:
            ts_print(f"  {c['name']}: {c['detail']}")
    return False, [str(gate_path)]
