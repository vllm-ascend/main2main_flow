"""Single-version pre-CI gate contract (main2main_flow).

The flow validates UT and mypy against the SINGLE main vllm version only
(both at every step's pre-CI via run_check and at the final quality
gate).  These tests pin that contract: no release-tree parameters may
creep back into the signatures, and run_check must aggregate the
mypy/ut checks it runs when vllm_path is given.
"""
from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

import pytest

from main2main_flow.scripts.utils import pre_ci_check
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
