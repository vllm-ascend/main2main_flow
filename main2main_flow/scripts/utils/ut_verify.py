"""Targeted UT verification for the adapter's fix mode (the verify loop).

pre_ci's check_ut runs the whole CPU-UT batch and returns only FAILED
one-liners plus ~900-char traceback excerpts.  Fixing a contract-drift
family needs the opposite loop: run JUST the failing files with full
tracebacks (--tb=long), observe, fix, repeat.  This CLI reproduces the
exact pre_ci execution environment (pure-CPU env, mocked npu-smi,
ut_namespace plugin, persistent venv from MAIN2MAIN_UT_VENV) so the
adapter verifies in seconds instead of editing blind.

This is the adapter's verify-loop command (SKILL.md fix mode); the flow
itself never calls it.  Exit code mirrors pytest's.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from main2main_flow.scripts.agent.toolguard.guard import GUARD_DIR
from main2main_flow.scripts.utils.ut_check import (
    UT_VERIFY_LOG_NAME,
    _UT_VENV_MARKER,
    _build_ut_env,
    _ensure_ut_venv,
    _make_fake_npu_smi,
    _triton_numpy_spec,
    _ut_base_dir,
    strip_ansi,
)
from main2main_flow.scripts.utils.utils import ts_print

_TAIL_LINES = 200

_BLOCKER_MARKER = "BLOCKED by main2main_flow"


def _failed_if_guard_blocked(code: int, out: str) -> int:
    """Blockers exit 0 by design (so the adapter doesn't retry them); if
    that output leaked into THIS run, the pass would be fake — force it to
    a failure (run 34046694076: exit=0 (0s) with zero tests executed)."""
    if code == 0 and _BLOCKER_MARKER in out:
        ts_print("[ut_verify] tool-guard blocker output leaked into the "
                 "pytest run — reporting failure, not a pass")
        return 1
    return code


def _resolve_pytest_cmd(explicit_python: str) -> list[str] | None:
    if explicit_python:
        p = Path(explicit_python)
        if p.exists():
            return [str(p), "-m", "pytest"]
        ts_print(f"[ut_verify] WARNING --python {explicit_python} does not "
                 "exist — falling back to the persistent venv")
    venv_python = _ut_base_dir() / "bin" / "python"
    if venv_python.exists():
        # Same source of truth as ut_check._ensure_ut_venv: a venv without
        # a readable marker was left behind by a failed creation (e.g.
        # numpy install failure) and must NOT be adopted.
        try:
            json.loads((_ut_base_dir() / _UT_VENV_MARKER).read_text(
                encoding="utf-8"))
            return [str(venv_python), "-m", "pytest"]
        except Exception:
            ts_print("[ut_verify] WARNING persistent venv has no valid "
                     "marker (stale/incomplete) — recreating it")
    # Adapter sessions run under the tool guard: PATH's `pytest` is the
    # exit-0 BLOCKER there (run 34046694076: ut_verify "passed" with zero
    # tests three times before the adapter gave up on verification).
    # Build the same venv pre_ci uses instead; only a REAL system pytest
    # (outside GUARD_DIR) is acceptable as fallback.
    _, venv_py = _ensure_ut_venv(_triton_numpy_spec())
    if venv_py:
        return [venv_py, "-m", "pytest"]
    guard_abs = os.path.abspath(str(GUARD_DIR))
    for d in os.environ.get("PATH", "").split(os.pathsep):
        if not d or os.path.abspath(d) == guard_abs:
            continue
        cand = Path(d) / "pytest"
        if cand.is_file() and os.access(cand, os.X_OK):
            return [str(cand)]
    ts_print("[ut_verify] ERROR no usable pytest: the tool guard shadows "
             "the system pytest and the persistent venv is unavailable — "
             "pass --python <venv_python from pre_ci_check.json>")
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m main2main_flow.scripts.utils.ut_verify",
        description="Run the given tests/ut files in the pre_ci UT "
                    "environment (pure CPU, mocked npu-smi) with full "
                    "tracebacks — the adapter's verify loop.")
    parser.add_argument("--repo", required=True,
                        help="vllm-ascend checkout (pytest cwd)")
    parser.add_argument("--vllm", required=True, help="vLLM checkout")
    parser.add_argument("--python", default="",
                        help="venv python to use (from pre_ci_check.json "
                             "venv_python); falls back to the persistent "
                             "UT venv, then system pytest")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("files", nargs="+",
                        help="tests/ut file paths relative to --repo")
    args = parser.parse_args(argv)

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"[ut_verify] ERROR repo not found: {repo}", file=sys.stderr)
        return 2
    missing = [f for f in args.files if not (repo / f).exists()]
    if missing:
        print(f"[ut_verify] ERROR files not under {repo}: {missing}",
              file=sys.stderr)
        return 2

    pytest_cmd = _resolve_pytest_cmd(args.python)
    if pytest_cmd is None:
        return 2
    fake_bin_dir = _make_fake_npu_smi()
    try:
        env = _build_ut_env(repo, args.vllm, fake_bin_dir)
        cmd = [*pytest_cmd, "-q", "--tb=long", "--no-header",
               "--continue-on-collection-errors",
               "-p", "main2main_flow.scripts.utils.ut_namespace",
               *args.files]
        started = time.monotonic()
        try:
            rr = subprocess.run(
                cmd, cwd=str(repo), capture_output=True, text=True,
                env=env, timeout=args.timeout,
            )
        except subprocess.TimeoutExpired as e:
            out = strip_ansi((e.stdout or "") + (e.stderr or ""))
            ts_print(f"[ut_verify] TIMEOUT after {args.timeout}s")
            code = 124
        else:
            out = strip_ansi(rr.stdout + rr.stderr)
            code = rr.returncode

        code = _failed_if_guard_blocked(code, out)

        log_path = _ut_base_dir() / UT_VERIFY_LOG_NAME
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "# ut_verify full log\n"
            f"# cmd: {' '.join(cmd)}\n"
            f"# repo: {repo}\n"
            f"# PYTHONPATH: {env['PYTHONPATH']}\n"
            + out,
            encoding="utf-8")

        tail = "\n".join(out.splitlines()[-_TAIL_LINES:])
        print(tail)
        print(f"\n[ut_verify] exit={code} "
              f"({time.monotonic() - started:.0f}s) — full log: {log_path}")
        return code
    finally:
        shutil.rmtree(fake_bin_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
