"""Tool guard: ALL in-session test/lint execution is blocked (5-15min each,
kills the session) — including the former sanctioned ut_verify entry, which
was removed with the decision that only the flow's pre_ci may execute
pre_ci tests (run 34046694076: ut_verify "passed" 3× with zero tests under
the guard's own exit-0 blockers; the adapter must not execute tests at all).
"""
import subprocess
import sys
from pathlib import Path

from main2main_flow.scripts.agent.toolguard.guard import (
    GUARD_DIR,
    GUARD_MSG,
    ensure_tool_guard,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _run_wrapper(module: str, *args: str) -> subprocess.CompletedProcess:
    ensure_tool_guard()
    return subprocess.run(
        [sys.executable, str(GUARD_DIR / "python"), "-m", module, *args],
        capture_output=True, text=True, cwd=_REPO_ROOT, timeout=60)


def test_guard_blocks_direct_pytest():
    r = _run_wrapper("pytest", "--version")
    assert "BLOCKED" in r.stdout
    assert r.returncode == 0  # exit 0: not a fixable failure


def test_guard_blocks_direct_mypy():
    r = _run_wrapper("mypy", "--version")
    assert "BLOCKED" in r.stdout


def _run_python_wrapper(*args: str) -> subprocess.CompletedProcess:
    ensure_tool_guard()
    return subprocess.run(
        [sys.executable, str(GUARD_DIR / "python"), *args],
        capture_output=True, text=True, cwd=_REPO_ROOT, timeout=60)


def test_guard_blocks_glued_m_flag():
    # `python -mpytest` is valid CPython — argv[1]=="-m" checks miss it.
    r = _run_python_wrapper("-mpytest", "--version")
    assert "BLOCKED" in r.stdout
    assert r.returncode == 0


def test_guard_blocks_isolation_flag_form():
    # `python -I -m pytest`: flags may precede -m.
    r = _run_python_wrapper("-I", "-m", "pytest", "--version")
    assert "BLOCKED" in r.stdout


def test_guard_blocks_py_c_invocation():
    # `python -c "import pytest; ..."` runs the suite without -m at all.
    r = _run_python_wrapper("-c", "import pytest; print('ran')")
    assert "BLOCKED" in r.stdout


def test_guard_passes_plain_python_through():
    r = _run_python_wrapper("-c", "print('hello')")
    assert "BLOCKED" not in r.stdout
    assert "hello" in r.stdout


def test_guard_message_is_plain_block():
    # No sanctioned in-session verifier exists anymore — the message must
    # not point the adapter at any executable entry point.
    assert "ut_verify" not in GUARD_MSG


def test_guard_blocks_ut_verify_module():
    # `python -m main2main_flow.scripts.utils.ut_verify` must hit the
    # wrapper's -m check (exact module-name match).
    r = _run_wrapper("main2main_flow.scripts.utils.ut_verify", "--help")
    assert "BLOCKED" in r.stdout
    assert r.returncode == 0


def test_guard_blocks_ut_verify_import_via_c():
    # `python -c "from ... import ut_verify; ..."` is caught by the
    # substring scan over the -c payload.
    r = _run_python_wrapper(
        "-c",
        "from main2main_flow.scripts.utils import ut_verify; "
        "ut_verify.main(['--repo', 'x'])")
    assert "BLOCKED" in r.stdout
    assert r.returncode == 0
