"""Single-version pre-CI gate contract (main2main_flow).

The flow validates UT and mypy against the SINGLE main vllm version only
(both at every step's pre-CI via run_check and at the final quality
gate).  These tests pin that contract: no release-tree parameters may
creep back into the signatures, and run_check must aggregate the
mypy/ut checks it runs when vllm_path is given.
"""
from __future__ import annotations

import inspect
import json
import subprocess
import sys
from pathlib import Path

import pytest

from main2main_flow.scripts.utils import pre_ci_check
from main2main_flow.scripts.utils import ut_check
from main2main_flow.scripts.utils.final_quality_gate import run_final_quality_gate
from main2main_flow.scripts.utils.ut_check import check_ut


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(repo), check=True,
                       capture_output=True, text=True)
    git("init", "-q")
    git("checkout", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / "vllm_ascend").mkdir()
    (repo / "vllm_ascend" / "code.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")


def test_check_ut_single_version_signature() -> None:
    params = inspect.signature(check_ut).parameters
    assert "vllm_release_path" not in params
    assert "release_tag" not in params
    assert "vllm_path" in params


def test_check_mypy_single_version_signature() -> None:
    params = inspect.signature(pre_ci_check._check_mypy).parameters
    assert "vllm_release_path" not in params


def test_run_check_without_vllm_path_skips_mypy_ut(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)
    called = []
    monkeypatch.setattr(pre_ci_check, "_check_mypy",
                        lambda *a, **k: called.append("mypy") or {"violations": []})
    monkeypatch.setattr(pre_ci_check, "_check_ut",
                        lambda *a, **k: called.append("ut") or {"violations": []})
    result = pre_ci_check.run_check(repo, "v0.27.1")
    assert result["all_passed"] is True
    assert called == []
    assert [c["name"] for c in result["checks"]] == [
        "version_strings", "temp_files", "format"]


def test_run_check_runs_mypy_ut_with_vllm_path(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)
    monkeypatch.setattr(pre_ci_check, "_check_mypy",
                        lambda repo_arg, vllm_arg: {
                            "violations": ["a.py:1: error: boom"],
                            "detail": "1 mypy issue(s)"})
    monkeypatch.setattr(pre_ci_check, "_check_ut",
                        lambda repo_arg, vllm_arg: {
                            "violations": [], "detail": "UT clean"})
    result = pre_ci_check.run_check(repo, "v0.27.1", vllm_path=tmp_path / "vllm")
    assert result["all_passed"] is False
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["mypy"]["passed"] is False
    assert by_name["ut"]["passed"] is True
    assert "skipped" not in by_name["ut"] or by_name["ut"]["skipped"] is False


def test_run_check_mypy_ut_skipped_never_fails(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)
    monkeypatch.setattr(pre_ci_check, "_check_mypy",
                        lambda *a, **k: {"violations": [], "detail": "",
                                         "skipped": True})
    monkeypatch.setattr(pre_ci_check, "_check_ut",
                        lambda *a, **k: {"violations": [], "detail": "",
                                         "skipped": True})
    result = pre_ci_check.run_check(repo, "v0.27.1", vllm_path=tmp_path / "vllm")
    assert result["all_passed"] is True


def test_final_quality_gate_single_version_signature() -> None:
    params = inspect.signature(run_final_quality_gate).parameters
    assert "vllm_release_path" not in params
    assert "release_tag" not in params


def test_extract_error_block_collection_error() -> None:
    # A collection ImportError sits in the MIDDLE of the output, while
    # the tail is pytest-asyncio deprecation noise.  The extractor must
    # return the ERROR paragraph (with the traceback), not the tail —
    # run 33538038959's gate adapter saw only the warning tail for 3
    # fix rounds and never the real ImportError.
    output = (
        "collected 246 files / 1 error\n"
        "===================== ERRORS =====================\n"
        "___________ ERROR collecting tests/ut/worker/test_attn_utils_v2.py ____________\n"
        "tests/ut/worker/test_attn_utils_v2.py:42: in <module>\n"
        "    from vllm.v1.worker.gpu_model_runner import GPUModelRunner\n"
        "E   ImportError: cannot import name 'GPUModelRunner'\n"
        "\n"
        "________ ERROR at setup of test_x ________\n"
        "some other block\n"
        "\n"
        "warnings.warn(PytestDeprecationWarning(_DEFAULT_FIXTURE_LOOP_SCOPE_UNSET))\n"
    )
    block = ut_check._extract_error_block(output)
    assert "ERROR collecting tests/ut/worker/test_attn_utils_v2.py" in block
    assert "ImportError: cannot import name 'GPUModelRunner'" in block
    # The error paragraph ends at the next pytest section marker; the
    # deprecation-warning tail must not leak in.
    assert "PytestDeprecationWarning" not in block


def test_extract_error_block_none_returns_empty() -> None:
    assert ut_check._extract_error_block("all passed\n") == ""


def test_junitxml_structured(tmp_path: Path) -> None:
    # End-to-end: pytest's built-in junitxml must capture BOTH a failed
    # test and a collection error with full tracebacks — the structured
    # route that text parsing (run 33538038959) missed.
    tdir = tmp_path / "proj"
    (tdir / "tests").mkdir(parents=True)
    (tdir / "tests" / "test_fail.py").write_text(
        "def test_boom():\n    assert 1 == 2\n", encoding="utf-8")
    (tdir / "tests" / "test_coll.py").write_text(
        "import nonexistent_module_xyz\n", encoding="utf-8")
    report = tmp_path / "report.xml"
    r = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header",
         "--continue-on-collection-errors",
         f"--junitxml={report}", "tests/"],
        cwd=str(tdir), capture_output=True, text=True)
    assert r.returncode != 0
    vs = ut_check._violations_from_junit("main", "batch", report)
    # junitxml nodeid form: classname is dotted (tests.test_fail), name
    # is the test function — joined as classname::name.
    assert any("tests.test_fail::test_boom FAILED" in v
               and "assert 1 == 2" in v for v in vs)
    assert any("COLLECTION ERROR" in v and "ModuleNotFoundError" in v
               for v in vs)


def test_violations_from_junit(tmp_path: Path) -> None:
    xml = tmp_path / "r.xml"
    xml.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n'
        '<testsuites><testsuite errors="1" failures="1">'
        '<testcase classname="tests.a" name="t"><failure '
        'message="boom">tb1\ntb2\n</failure></testcase>'
        '<testcase classname="" name="tests.b"><error '
        'message="collection failure">ImportError: x\ntb3\n</error>'
        '</testcase></testsuite></testsuites>', encoding="utf-8")
    vs = ut_check._violations_from_junit("main", "batch", xml)
    assert len(vs) == 2
    assert "tests.a::t FAILED — boom" in vs[0]
    assert "tb2" in vs[0]
    assert "COLLECTION ERROR tests.b — collection failure" in vs[1]
    assert "ImportError: x" in vs[1]
    # Missing report -> [] (caller falls back to text parsing).
    assert ut_check._violations_from_junit("main", "batch",
                                           tmp_path / "none.xml") == []


def test_is_real_error_strips_workflow_error_prefix() -> None:
    # format.sh prints failing hook lines with ::error:: (runner shows
    # ##[error]); the prefix made every lint violation invisible and the
    # check reported OK — run 33784514899 shipped an E402 the upstream
    # pre-commit caught (2026-09-04).
    line = ("::error::tests/ut/worker/test_model_runner_v1.py:45:1: "
            "E402 Module level import not at top of file")
    assert pre_ci_check._is_real_error(line)
    line2 = ("##[error]tests/ut/worker/test_model_runner_v1.py:45:1: "
             "E402 Module level import not at top of file")
    assert pre_ci_check._is_real_error(line2)
    # Non-violation lines still filtered.
    assert not pre_ci_check._is_real_error("- hook id: ruff-check")
    assert not pre_ci_check._is_real_error("files were modified by this hook")
