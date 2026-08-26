"""Offline verification of the external E2E dispatcher (feature/e2e-external).

The parser (parse_exec_artifacts) must produce run_tests()-shaped results —
the same shapes the fix-mode contract and final gate consume — from the
exec workflow's per-chip artifacts.  Synthetic pytest logs mirror what
run_selected_tests.sh --timing writes to each test's log file.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from main2main_flow.scripts.utils import e2e_dispatch
from main2main_flow.scripts.utils.run_tests import build_test_errors_detail

PASSED_LOG = """\
============================= test session starts ==============================
collected 1 item
tests/e2e/a2/test_batch_invariant.py::test_basic PASSED
============================== 1 passed in 12.30s ==============================
"""

FAILED_LOG = """\
============================= test session starts ==============================
collected 1 item
tests/e2e/a2/test_foo.py::test_bar FAILED
_________________________________ test_bar __________________________________
def test_bar():
        raise RuntimeError("boom")
E       RuntimeError: boom
tests/e2e/a2/test_foo.py:10: RuntimeError
FAILED tests/e2e/a2/test_foo.py::test_bar - RuntimeError: boom
============================== 1 failed in 3.21s ==============================
"""


def _make_artifact_dir(tmp_path: Path, chip: str, num_npus: int,
                       entries: list[dict], elapsed: float = 42.0) -> Path:
    adir = tmp_path / f"main2main-e2e-round-1-{chip}"
    adir.mkdir(parents=True)
    tests = []
    for e in entries:
        if e["log"]:
            (adir / e["log"]).write_text(e["log_text"], encoding="utf-8")
        tests.append({k: e[k] for k in
                      ("name", "passed", "exit_code", "elapsed", "log",
                       "status")})
    (adir / "results.json").write_text(
        json.dumps({"chip": chip, "npu_type": chip, "num_npus": num_npus,
                    "elapsed_s": elapsed, "tests": tests}),
        encoding="utf-8")
    return adir


def test_parse_artifacts_all_passed(tmp_path: Path) -> None:
    _make_artifact_dir(
        tmp_path, "a2", 8,
        [{"name": "tests/e2e/a2/test_batch_invariant.py", "passed": True,
          "exit_code": 0, "elapsed": 12.3, "log": "1-test_batch_invariant.log",
          "status": "PASSED", "log_text": PASSED_LOG}])
    result = e2e_dispatch.parse_exec_artifacts(tmp_path, 1, 0)
    assert result["ci_result"] == "passed"
    assert result["can_commit"] is True
    assert result["tests"] == ["tests/e2e/a2/test_batch_invariant.py"]
    assert result["elapsed_s"] == 42.0
    assert result["total_cards"] == 8
    assert result["suite_results"]["tests/e2e/a2/test_batch_invariant.py"][
        "ci_result"] == "passed"


def test_parse_artifacts_code_bug(tmp_path: Path) -> None:
    _make_artifact_dir(
        tmp_path, "a2", 8,
        [{"name": "tests/e2e/a2/test_foo.py", "passed": False,
          "exit_code": 1, "elapsed": 3.2, "log": "2-test_foo.log",
          "status": "FAILED", "log_text": FAILED_LOG}])
    result = e2e_dispatch.parse_exec_artifacts(tmp_path, 1, 0)
    assert result["ci_result"] == "failed"
    assert result["can_commit"] is False
    assert result["requires_fix"] is True
    tr = result["suite_results"]["tests/e2e/a2/test_foo.py"]
    assert tr["ci_result"] == "failed"
    assert tr["code_bugs_count"] == 1
    assert tr["run_suite_exit_code"] == 1
    assert tr["summary_error"] is None
    assert tr["not_run"] is False


def test_parse_artifacts_not_run(tmp_path: Path) -> None:
    _make_artifact_dir(
        tmp_path, "310p", 4,
        [{"name": "tests/e2e/310p/test_bar.py", "passed": False,
          "exit_code": 1, "elapsed": 0.0, "log": "", "status": "NOT_RUN",
          "log_text": ""}])
    result = e2e_dispatch.parse_exec_artifacts(tmp_path, 1, 0)
    tr = result["suite_results"]["tests/e2e/310p/test_bar.py"]
    assert tr["ci_result"] == "failed"
    assert tr["not_run"] is True
    detail = build_test_errors_detail(result["suite_results"], 1, tmp_path,
                                      tmp_path / "round-1-result.json")
    text = detail.read_text(encoding="utf-8")
    assert "was NOT run by the E2E job" in text
    assert "[log excerpt]" not in text


def test_parse_artifacts_missing_chip_drops(tmp_path: Path) -> None:
    # results.json unparseable -> chip dropped; empty suite -> summary_error.
    adir = tmp_path / "main2main-e2e-round-1-a3"
    adir.mkdir()
    (adir / "results.json").write_text("{broken", encoding="utf-8")
    result = e2e_dispatch.parse_exec_artifacts(tmp_path, 1, 0)
    assert result["ci_result"] == "failed"
    assert result["can_commit"] is False
    assert result["summary_error"] == "no exec artifacts"


def test_aggregate_suite_results_byte_shape() -> None:
    # The aggregate must keep the exact field set run_tests() step-9 used
    # (fix mode + final gate read these keys).
    all_results = [
        {"test": "t1", "ci_result": "passed", "code_bugs_count": 0,
         "env_flakes_count": 0, "failed_test_files_count": 0,
         "failed_test_cases_count": 0},
        {"test": "t2", "ci_result": "env_flake_pass", "code_bugs_count": 0,
         "env_flakes_count": 2, "failed_test_files_count": 0,
         "failed_test_cases_count": 0},
    ]
    r = e2e_dispatch.aggregate_suite_results(
        0, 1, all_results, total_cards=8, sequential=False, remote=False,
        ci_dir=Path("/x"), rounds_info=[], total_elapsed=10.5)
    assert r["ci_result"] == "env_flake_pass"
    assert r["can_commit"] is True
    assert set(r) == {
        "step_id", "round", "label", "tests", "ci_result", "passed",
        "can_commit", "requires_fix", "log_path", "summary_path",
        "total_cards", "sequential", "remote", "elapsed_s", "rounds",
        "suite_results", "code_bugs_count", "env_flakes_count",
        "failed_test_files_count", "failed_test_cases_count",
    }
    mixed = all_results + [{"test": "t3", "ci_result": "failed",
                            "code_bugs_count": 1, "env_flakes_count": 0,
                            "failed_test_files_count": 1,
                            "failed_test_cases_count": 1}]
    r2 = e2e_dispatch.aggregate_suite_results(
        0, 1, mixed, total_cards=8, sequential=False, remote=False,
        ci_dir=Path("/x"), rounds_info=[], total_elapsed=10.5)
    assert r2["ci_result"] == "failed"
    assert r2["requires_fix"] is True


def test_rewrite_runner() -> None:
    cases = [
        ("linux-aarch64-a2b3-1", "linux-aarch64-a2b1-8"),
        ("linux-aarch64-a2b3-2", "linux-aarch64-a2b1-8"),
        ("linux-aarch64-a2b3-4", "linux-aarch64-a2b1-8"),
        ("linux-aarch64-a3-2", "linux-aarch64-a3-800i-16-cn12-001"),
        ("linux-aarch64-a3-4", "linux-aarch64-a3-800i-16-cn12-001"),
        ("linux-aarch64-a3-8", "linux-aarch64-a3-800i-16-cn12-001"),
        ("linux-aarch64-310p-1", "linux-aarch64-310p-4"),
        ("linux-aarch64-310p-2", "linux-aarch64-310p-4"),
        ("linux-aarch64-310p-4", "linux-aarch64-310p-4"),
    ]
    for src, want in cases:
        label, image_tag = e2e_dispatch._rewrite_runner(src)
        assert label == want, f"{src} -> {label} (want {want})"
        assert image_tag, f"{src}: image_tag must be set"
    # Unknown runners pass through untouched.
    label, image_tag = e2e_dispatch._rewrite_runner("linux-amd64-cpu-8-hk")
    assert label == "linux-amd64-cpu-8-hk" and image_tag == ""


def test_compute_test_groups(monkeypatch, tmp_path: Path) -> None:
    select_script = tmp_path / ".github/workflows/scripts/select_tests.py"
    select_script.parent.mkdir(parents=True)
    select_script.write_text("#!/bin/false\n", encoding="utf-8")
    groups = [
        {"num_npus": 1, "npu_type": "a2", "runner": "linux-aarch64-a2b3-1",
         "tests": "tests/e2e/a2/test_x.py", "partition": "1-1",
         "image_tag": "9.1.0-910b-ubuntu22.04-py3.12"},
        {"num_npus": 4, "npu_type": "a3", "runner": "linux-aarch64-a3-4",
         "tests": "tests/e2e/a3_4/test_y.py", "partition": "1-1"},
        {"num_npus": 0, "npu_type": "cpu", "runner": "linux-amd64-cpu-8-hk",
         "tests": "tests/ut/a2/test_ut.py", "partition": "1-1"},
    ]
    payload = json.dumps(groups, separators=(",", ":"))
    env = {"GITHUB_OUTPUT": ""}

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0,
            stdout=(f"test_groups={payload}\nhas_tests=true\n"
                    f"csrc_cache_target_ids=[]\nmatched_modules=\n"),
            stderr="")

    monkeypatch.setattr(e2e_dispatch.subprocess, "run", fake_run)
    result = e2e_dispatch.compute_test_groups(tmp_path, "abc123", ["vllm/x.py"])
    assert len(result) == 2  # cpu group dropped
    assert result[0]["runner"] == "linux-aarch64-a2b1-8"
    assert result[0]["image_tag"] == "9.1.0-910b-ubuntu22.04-py3.12"
    assert result[1]["runner"] == "linux-aarch64-a3-800i-16-cn12-001"
    assert result[1]["image_tag"] == "9.1.0-a3-ubuntu22.04-py3.12"


def test_compute_test_groups_select_tests_missing(tmp_path: Path) -> None:
    try:
        e2e_dispatch.compute_test_groups(tmp_path, "abc", [])
        raise AssertionError("expected RuntimeError")
    except RuntimeError:
        pass


def test_build_test_errors_detail_contract(tmp_path: Path) -> None:
    adir = _make_artifact_dir(
        tmp_path, "a2", 8,
        [{"name": "tests/e2e/a2/test_foo.py", "passed": False,
          "exit_code": 1, "elapsed": 3.2, "log": "2-test_foo.log",
          "status": "FAILED", "log_text": FAILED_LOG}])
    result = e2e_dispatch.parse_exec_artifacts(tmp_path, 1, 0)
    ci_dir = tmp_path / "main2main-e2e-round-1-a2"
    detail = build_test_errors_detail(result["suite_results"], 1, ci_dir,
                                      ci_dir / "round-1-result.json")
    assert detail is not None
    text = detail.read_text(encoding="utf-8")
    assert "=== tests/e2e/a2/test_foo.py ===" in text
    assert "[log excerpt]" in text
    assert "[summary]" in text
    assert "RuntimeError: boom" in text
    # Passed tests never appear.
    assert "test_batch_invariant" not in text


def test_build_test_errors_detail_no_failures(tmp_path: Path) -> None:
    detail = build_test_errors_detail({}, 1, tmp_path,
                                      tmp_path / "round-1-result.json")
    assert detail is None
