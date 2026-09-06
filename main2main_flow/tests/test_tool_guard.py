"""Tool guard: direct test/lint commands are blocked (5-15min each, kills the
session), but the sanctioned ut_verify entry passes through.

run 34018086282: attempt-1 made 54 blind edits with ZERO verification — the
guard message said "tests are forbidden" and SKILL.md only sanctioned
ut_verify in fix mode, so the adapter never discovered the one check it was
allowed to run.  The block message must now REDIRECT to ut_verify.
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


def test_guard_message_redirects_to_ut_verify():
    assert "ut_verify" in GUARD_MSG


def test_guard_passes_ut_verify_through():
    # the sanctioned verifier must reach the real module (argparse --help)
    r = _run_wrapper("main2main_flow.scripts.utils.ut_verify", "--help")
    combined = r.stdout + r.stderr
    assert "BLOCKED" not in combined
    assert r.returncode == 0
    assert "--repo" in combined
