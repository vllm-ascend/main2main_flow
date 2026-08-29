"""Offline verification of the external E2E dispatcher (feature/e2e-external).

The parser (parse_exec_artifacts) must produce run_tests()-shaped results —
the same shapes the fix-mode contract and final gate consume — from the
resident runners' per-chip results branches.  Synthetic pytest logs mirror
what run_selected_tests.sh --timing writes to each test's log file.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

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
    suite = {}
    for e in entries:
        if e["log"]:
            # Log files carry the parse-side slug naming (round-<N>-<slug>.log)
            # so build_test_errors_detail can excerpt them.
            slug = e["name"].replace("/", "__").replace(".py", "")
            (adir / f"round-1-{slug}.log").write_text(
                e["log_text"], encoding="utf-8")
            if not e["passed"]:
                (adir / f"round-1-{slug}-summary.json").write_text(
                    json.dumps({"failed_test_files_count": 1,
                                "failed_test_cases_count": 1,
                                "code_bugs": ["boom"]}),
                    encoding="utf-8")
        passed = e["passed"]
        suite[e["name"]] = {
            "cards_required": num_npus,
            "run_suite_exit_code": 0 if passed else 1,
            "ci_result": "passed" if passed else "failed",
            "summary_error": None,
            "code_bugs_count": 0 if passed else 1,
            "env_flakes_count": 0,
            "failed_test_files_count": 0 if passed else 1,
            "failed_test_cases_count": 0 if passed else 1,
            "not_run": e["status"] == "NOT_RUN",
        }
    (adir / "round-1-result.json").write_text(
        json.dumps({"can_commit": all(e["passed"] for e in entries),
                    "ci_result": ("passed" if all(e["passed"] for e in entries)
                                  else "failed"),
                    "suite_results": suite, "rounds": [],
                    "elapsed_s": elapsed}),
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
    # round-N-result.json unparseable -> chip dropped; empty suite ->
    # summary_error.
    adir = tmp_path / "main2main-e2e-round-1-a3"
    adir.mkdir()
    (adir / "round-1-result.json").write_text("{broken", encoding="utf-8")
    result = e2e_dispatch.parse_exec_artifacts(tmp_path, 1, 0)
    assert result["ci_result"] == "failed"
    assert result["can_commit"] is False
    assert result["summary_error"] == "no exec artifacts"


def test_parse_artifacts_truncated_result_json_backfills(
        tmp_path: Path) -> None:
    # A run_tests.py killed mid-write publishes a partial result json; the
    # chip must fall into the NOT_RUN backfill (via expected_tests.json)
    # instead of being dropped — dropping it would let the other chip's
    # pass become an overall pass.
    adir = tmp_path / "main2main-e2e-round-1-a2"
    adir.mkdir()
    (adir / "round-1-result.json").write_text(
        '{"suite_results": {"tests/e2e/a2', encoding="utf-8")
    (adir / "expected_tests.json").write_text(json.dumps(
        [{"test": "tests/e2e/a2/test_x.py", "cards_required": 1}]),
        encoding="utf-8")
    result = e2e_dispatch.parse_exec_artifacts(tmp_path, 1, 0)
    tr = result["suite_results"]["tests/e2e/a2/test_x.py"]
    assert tr["ci_result"] == "failed"
    assert tr["not_run"] is True
    assert result["can_commit"] is False


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
        ("linux-aarch64-a2b3-1", "linux-aarch64-a2-1"),
        ("linux-aarch64-a2b3-2", "linux-aarch64-a2-1"),
        ("linux-aarch64-a2b3-4", "linux-aarch64-a2-1"),
        ("linux-aarch64-a3-2", "linux-aarch64-a3-800i-2-cn12-001"),
        ("linux-aarch64-a3-4", "linux-aarch64-a3-800i-2-cn12-001"),
        ("linux-aarch64-a3-8", "linux-aarch64-a3-800i-2-cn12-001"),
        ("linux-aarch64-310p-1", "linux-aarch64-310p-1"),
        ("linux-aarch64-310p-2", "linux-aarch64-310p-1"),
        ("linux-aarch64-310p-4", "linux-aarch64-310p-1"),
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
    monkeypatch.setenv("MAIN2MAIN_E2E_CHIPS", "a2,a3,310p")  # keep a3 for rewrite coverage
    result = e2e_dispatch.compute_test_groups(tmp_path, ["vllm/x.py"])
    assert len(result) == 2  # cpu group dropped
    assert result[0]["runner"] == "linux-aarch64-a2-1"
    assert result[0]["image_tag"] == "9.1.0-910b-ubuntu22.04-py3.12"
    assert result[1]["runner"] == "linux-aarch64-a3-800i-2-cn12-001"
    assert result[1]["image_tag"] == "9.1.0-a3-ubuntu22.04-py3.12"


def test_compute_test_groups_drops_non_resident_chips(
        monkeypatch, tmp_path: Path) -> None:
    # Default allowlist (a2,a3,310p) mirrors the resident matrix — all
    # NPU groups pass through.
    monkeypatch.delenv("MAIN2MAIN_E2E_CHIPS", raising=False)
    groups = [
        {"num_npus": 1, "npu_type": "a2", "runner": "linux-aarch64-a2b3-1",
         "tests": "tests/e2e/a2/test_x.py", "partition": "1-1"},
        {"num_npus": 4, "npu_type": "a3", "runner": "linux-aarch64-a3-4",
         "tests": "tests/e2e/a3_4/test_y.py", "partition": "1-1"},
    ]
    select_script = tmp_path / ".github/workflows/scripts/select_tests.py"
    select_script.parent.mkdir(parents=True)
    select_script.write_text("#!/bin/false\n", encoding="utf-8")
    payload = json.dumps(groups, separators=(",", ":"))

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout=f"test_groups={payload}\n", stderr="")

    monkeypatch.setattr(e2e_dispatch.subprocess, "run", fake_run)
    result = e2e_dispatch.compute_test_groups(tmp_path, ["vllm/x.py"])
    assert [g["npu_type"] for g in result] == ["a2", "a3"]
    assert result[1]["runner"] == "linux-aarch64-a3-800i-2-cn12-001"


def test_compute_test_groups_empty_changed_files(tmp_path: Path) -> None:
    # No changed files -> nothing to match, return [] without touching select_tests.py.
    assert e2e_dispatch.compute_test_groups(tmp_path, []) == []


def test_compute_test_groups_missing_test_groups_line(monkeypatch,
                                                      tmp_path: Path) -> None:
    # select_tests exits 0 but emits no test_groups= line — a broken
    # matcher must FAIL, not be mistaken for "no tests to run".
    select_script = tmp_path / ".github/workflows/scripts/select_tests.py"
    select_script.parent.mkdir(parents=True)
    select_script.write_text("#!/bin/false\n", encoding="utf-8")

    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd, 0, stdout="matched_modules=\nhas_tests=false\n", stderr="")

    monkeypatch.setattr(e2e_dispatch.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="no test_groups= line"):
        e2e_dispatch.compute_test_groups(tmp_path, ["vllm/x.py"])


def test_apply_minimal_filter_no_env() -> None:
    groups = [{"npu_type": "a2", "num_npus": 1, "tests": "a.py b.py"}]
    assert e2e_dispatch.apply_minimal_filter(groups) == groups


def test_apply_minimal_filter_selects(monkeypatch) -> None:
    groups = [
        {"npu_type": "a2", "num_npus": 1, "runner": "linux-aarch64-a2b1-8",
         "tests": "tests/e2e/a2/test_qwen3_0_6b.py "
                  "tests/e2e/a2/test_eagle.py tests/e2e/a2/test_dflash.py"},
        {"npu_type": "a3", "num_npus": 2, "runner": "linux-aarch64-a3-800i-16-cn12-001",
         "tests": "tests/e2e/a3_2/test_prefix_caching.py"},
        {"npu_type": "a3", "num_npus": 4, "runner": "linux-aarch64-a3-800i-16-cn12-001",
         "tests": "tests/e2e/a3_4/test_pipeline_parallel.py "
                  "tests/e2e/a3_4/test_data_parallel_tp2.py"},
    ]
    monkeypatch.setenv(
        "MAIN2MAIN_E2E_MINIMAL",
        "a2: test_eagle.py test_dflash.py\na3: test_prefix_caching.py test_pipeline_parallel.py")
    out = e2e_dispatch.apply_minimal_filter(groups)
    chips = [g["npu_type"] for g in out]
    assert chips == ["a2", "a3", "a3"]
    assert out[0]["tests"].split() == [
        "tests/e2e/a2/test_dflash.py", "tests/e2e/a2/test_eagle.py"]
    # 2-card group keeps only the matching test; 4-card group drops the rest
    assert out[1]["tests"] == "tests/e2e/a3_2/test_prefix_caching.py"
    assert out[2]["tests"] == "tests/e2e/a3_4/test_pipeline_parallel.py"


def test_apply_minimal_filter_unknown_chip_dropped(monkeypatch) -> None:
    groups = [
        {"npu_type": "310p", "num_npus": 1, "tests": "tests/e2e/310p/test_z.py"},
        {"npu_type": "a2", "num_npus": 1, "tests": "tests/e2e/a2/test_x.py"},
    ]
    monkeypatch.setenv("MAIN2MAIN_E2E_MINIMAL", "a2: test_x.py")
    out = e2e_dispatch.apply_minimal_filter(groups)
    assert [g["npu_type"] for g in out] == ["a2"]


def test_apply_minimal_filter_cross_chip_override(monkeypatch) -> None:
    # one_card tests route to a2 in the ready-all groups; a chip line may
    # claim them anyway (regrouped under the named chip, original num_npus
    # kept) — how single-card cases land on the a3 resident.  The same
    # test may appear under several chips.
    groups = [
        {"npu_type": "a2", "num_npus": 1, "runner": "linux-aarch64-a2-1",
         "image_tag": "9.1.0-910b-ubuntu22.04-py3.12",
         "tests": "tests/e2e/pull_request/one_card/test_qwen3_0_6b.py "
                  "tests/e2e/pull_request/one_card/test_sampler.py"},
        {"npu_type": "a3", "num_npus": 2, "runner": "linux-aarch64-a3-800i-2-cn12-001",
         "image_tag": "9.1.0-a3-ubuntu22.04-py3.12",
         "tests": "tests/e2e/pull_request/two_card/test_gemma4.py"},
    ]
    monkeypatch.setenv(
        "MAIN2MAIN_E2E_MINIMAL",
        "a2: test_qwen3_0_6b.py test_sampler.py\n"
        "a3: test_qwen3_0_6b.py test_sampler.py test_gemma4.py")
    out = e2e_dispatch.apply_minimal_filter(groups)
    assert [(g["npu_type"], g["num_npus"]) for g in out] == [
        ("a2", 1), ("a3", 1), ("a3", 2)]
    a3_singles = out[1]
    assert a3_singles["runner"] == "linux-aarch64-a2-1"  # metadata from source group
    assert a3_singles["tests"].split() == [
        "tests/e2e/pull_request/one_card/test_qwen3_0_6b.py",
        "tests/e2e/pull_request/one_card/test_sampler.py"]
    assert out[2]["tests"] == "tests/e2e/pull_request/two_card/test_gemma4.py"
    # The a2 line is untouched: same tests, original a2 group.
    assert out[0]["npu_type"] == "a2"
    assert out[0]["runner"] == "linux-aarch64-a2-1"


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


def _full_groups() -> list[dict]:
    return [
        {"num_npus": 1, "npu_type": "a2", "runner": "linux-aarch64-a2b1-8",
         "tests": "tests/e2e/a2/test_a.py tests/e2e/a2/test_b.py",
         "partition": "1-1", "image_tag": "9.1.0-910b-ubuntu22.04-py3.12"},
        {"num_npus": 4, "npu_type": "a3", "runner": "linux-aarch64-a3-800i-16-cn12-001",
         "tests": "tests/e2e/a3_4/test_c.py", "partition": "1-1",
         "image_tag": "9.1.0-a3-ubuntu22.04-py3.12"},
    ]


def test_incremental_groups_failing_only(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(e2e_dispatch, "run_git", lambda *a, **k: "")
    result = e2e_dispatch.incremental_test_groups(
        tmp_path, "abc123", ["tests/e2e/a2/test_b.py"], _full_groups())
    assert result is not None
    assert len(result) == 1
    assert result[0]["tests"] == "tests/e2e/a2/test_b.py"  # group shrunk
    assert result[0]["npu_type"] == "a2"
    assert result[0]["num_npus"] == 1  # routing metadata preserved
    assert result[0]["image_tag"] == "9.1.0-910b-ubuntu22.04-py3.12"


def test_incremental_groups_with_diff_impact(monkeypatch,
                                             tmp_path: Path) -> None:
    monkeypatch.setattr(e2e_dispatch, "run_git",
                        lambda *a, **k: "vllm_ascend/foo.py\n")
    monkeypatch.setattr(
        e2e_dispatch, "_map_changed_to_tests",
        lambda *a, **k: ["tests/e2e/a3_4/test_c.py",
                         "tests/ut/attention/not_in_gpu_suite.py"])
    result = e2e_dispatch.incremental_test_groups(
        tmp_path, "abc123", ["tests/e2e/a2/test_a.py"], _full_groups())
    assert result is not None
    by_type = {g["npu_type"]: g["tests"] for g in result}
    assert "tests/e2e/a2/test_a.py" in by_type["a2"]
    # impact ∪ failing, deduped; routed via the full groups (test_c keeps
    # its a3 x4 routing even though it was also a failing test).  The UT
    # target has no group in the NPU suite → dropped (quality gate covers).
    assert by_type["a3"] == "tests/e2e/a3_4/test_c.py"
    for g in result:
        assert g["npu_type"] in ("a2", "a3")
        assert g["num_npus"] in (1, 4)


def test_incremental_groups_drops_unmapped_targets(monkeypatch,
                                                   tmp_path: Path) -> None:
    # CPU-UT targets (not in the NPU full groups) must not leak into the
    # fix round — the quality gate covers them.
    monkeypatch.setattr(e2e_dispatch, "run_git",
                        lambda *a, **k: "vllm_ascend/foo.py\n")
    monkeypatch.setattr(e2e_dispatch, "_map_changed_to_tests",
                        lambda *a, **k: ["tests/ut/attention/test_x.py"])
    result = e2e_dispatch.incremental_test_groups(
        tmp_path, "abc123", ["tests/e2e/a2/test_b.py"], _full_groups())
    assert result is not None
    assert len(result) == 1
    assert result[0]["tests"] == "tests/e2e/a2/test_b.py"


def test_incremental_groups_unmappable_failing(monkeypatch,
                                               tmp_path: Path) -> None:
    monkeypatch.setattr(e2e_dispatch, "run_git", lambda *a, **k: "")
    result = e2e_dispatch.incremental_test_groups(
        tmp_path, "abc123", ["tests/e2e/a2/never_scheduled.py"],
        _full_groups())
    assert result is None


def test_incremental_groups_mapping_failure(monkeypatch,
                                            tmp_path: Path) -> None:
    monkeypatch.setattr(e2e_dispatch, "run_git",
                        lambda *a, **k: "vllm_ascend/foo.py\n")
    def boom(*a, **k):
        raise RuntimeError("yaml broken")
    monkeypatch.setattr(e2e_dispatch, "_map_changed_to_tests", boom)
    # Impact mapping fails → failing set alone still covers completeness.
    result = e2e_dispatch.incremental_test_groups(
        tmp_path, "abc123", ["tests/e2e/a2/test_b.py"], _full_groups())
    assert result is not None
    assert result[0]["tests"] == "tests/e2e/a2/test_b.py"


# ---- command protocol: push_signal_branch / wait_chip_results --------------
# Every E2E round is a force-push of the signal branch whose commit carries
# command.json = {"round", "main_run_id"}; the resident jobs serve it in
# place and push results to <signal_branch>_results_<chip>.  The flow
# materializes each chip's round-N/ from the results branch.

# ---- per-test progress relay (results-branch progress file -> main log) ----

def _init_progress_repo(repo: Path, events: list[dict],
                        round_number: int = 1) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(repo), check=True,
                       capture_output=True, text=True)
    git("init", "-q")
    git("checkout", "-q", "-b", "main2main_e2e_results_a2")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / f"round-{round_number}-progress.json").write_text(
        json.dumps({"round": round_number, "events": events}),
        encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "progress")


def _append_progress_event(repo: Path, event: dict,
                           round_number: int = 1) -> None:
    path = repo / f"round-{round_number}-progress.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["events"].append(event)
    path.write_text(json.dumps(data), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(repo), check=True,
                   capture_output=True, text=True)
    subprocess.run(["git", "commit", "-q", "-m", "progress+"], cwd=str(repo),
                   check=True, capture_output=True, text=True)


def test_relay_test_progress_streams_new_events(capsys, monkeypatch,
                                                tmp_path: Path) -> None:
    cfg = e2e_dispatch.E2EDispatchConfig(signal_branch="main2main_e2e")
    origin = tmp_path / "origin"
    _init_progress_repo(origin, [
        {"test": "tests/e2e/a2/test_x.py", "event": "started"}])
    monkeypatch.setattr(e2e_dispatch, "_signal_git_url",
                        lambda cfg: str(origin))
    state: dict = {}
    e2e_dispatch._relay_test_progress(cfg, 1, ["a2"], state)
    out = capsys.readouterr().out
    assert "[e2e-dispatch][a2] tests/e2e/a2/test_x.py started" in out
    assert "done" not in out
    # The resident appends events as the round runs; only new ones relay.
    _append_progress_event(origin, {
        "test": "tests/e2e/a2/test_x.py", "event": "done", "exit": 0,
        "result": "passed", "bugs": 0, "flakes": 0})
    e2e_dispatch._relay_test_progress(cfg, 1, ["a2"], state)
    out = capsys.readouterr().out
    assert ("[e2e-dispatch][a2] tests/e2e/a2/test_x.py done: exit=0, "
            "result=passed, bugs=0, flakes=0") in out
    assert "started" not in out
    e2e_dispatch._relay_test_progress(cfg, 1, ["a2"], state)
    assert capsys.readouterr().out == ""


def test_relay_test_progress_other_round_ignored(capsys, monkeypatch,
                                                 tmp_path: Path) -> None:
    # Progress from another round number must not be relayed as this
    # round's status.
    cfg = e2e_dispatch.E2EDispatchConfig(signal_branch="main2main_e2e")
    origin = tmp_path / "origin"
    _init_progress_repo(origin, [
        {"test": "tests/e2e/a2/test_x.py", "event": "done", "exit": 1,
         "result": "failed", "bugs": 1, "flakes": 0}], round_number=2)
    monkeypatch.setattr(e2e_dispatch, "_signal_git_url",
                        lambda cfg: str(origin))
    e2e_dispatch._relay_test_progress(cfg, 1, ["a2"], {})
    assert capsys.readouterr().out == ""


def test_relay_test_progress_tolerant_when_missing(capsys, monkeypatch,
                                                   tmp_path: Path) -> None:
    # Missing branch / progress file (nothing pushed yet) is the normal
    # pending state: silent, no crash, retried on the next poll.
    # _signal_git_url must be isolated: the real repo's results branches
    # are live while a main run is in flight.
    cfg = e2e_dispatch.E2EDispatchConfig(signal_branch="main2main_e2e")
    monkeypatch.setattr(e2e_dispatch, "_signal_git_url",
                        lambda cfg: str(tmp_path / "nowhere"))
    e2e_dispatch._relay_test_progress(
        cfg, 1, ["a2"], {})  # no branch anywhere
    assert capsys.readouterr().out == ""
    origin = tmp_path / "origin"
    _init_progress_repo(origin, [], round_number=3)  # branch, no round-1 file
    monkeypatch.setattr(e2e_dispatch, "_signal_git_url",
                        lambda cfg: str(origin))
    e2e_dispatch._relay_test_progress(cfg, 1, ["a2"], {})
    e2e_dispatch._relay_test_progress(cfg, 1, ["a2"], {})
    assert capsys.readouterr().out == ""


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(repo), check=True,
                       capture_output=True, text=True)
    git("init", "-q")
    git("checkout", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / "code.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")


def _git_show(repo: Path, spec: str) -> str:
    return subprocess.run(["git", "show", spec], cwd=str(repo), check=True,
                          capture_output=True, text=True).stdout


def test_push_signal_branch_carries_command(tmp_path: Path,
                                            monkeypatch) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)
    # Uncommitted tracked change must be carried into the snapshot.
    (repo / "code.py").write_text("x = 2\n", encoding="utf-8")
    pushed: dict = {}
    monkeypatch.setattr(
        e2e_dispatch, "_push_via_proxy",
        lambda wt, fork, refspec, *a: pushed.update(fork=fork,
                                                    refspec=refspec))
    # Verification seam: answer with the worktree HEAD (the pushed sha).
    monkeypatch.setattr(
        e2e_dispatch, "_remote_branch_sha",
        lambda wt, fork, branch: e2e_dispatch.run_git(
            wt, "rev-parse", "HEAD").strip())
    sha = e2e_dispatch.push_signal_branch(
        repo, "main2main_e2e", "fork/x", [{"npu_type": "a2"}], 3, "42")
    assert pushed == {"fork": "fork/x",
                      "refspec": "HEAD:refs/heads/main2main_e2e"}
    cmd = json.loads(_git_show(repo, f"{sha}:command.json"))
    assert cmd == {"round": 3, "main_run_id": "42"}
    groups = json.loads(_git_show(repo, f"{sha}:test_groups.json"))
    assert groups == [{"npu_type": "a2"}]
    assert _git_show(repo, f"{sha}:code.py") == "x = 2\n"


def test_push_signal_branch_retries_on_mismatch(tmp_path: Path,
                                                monkeypatch) -> None:
    # A push whose sha is not visible (or that raised) must be retried —
    # the force-push is idempotent, so verify-then-retry is safe.
    repo = tmp_path / "ascend"
    _init_repo(repo)
    pushes = {"n": 0}
    monkeypatch.setattr(
        e2e_dispatch, "_push_via_proxy",
        lambda wt, fork, refspec, *a: pushes.__setitem__("n", pushes["n"] + 1))
    verifies = {"n": 0}
    monkeypatch.setattr(
        e2e_dispatch, "_remote_branch_sha",
        lambda wt, fork, branch: (
            e2e_dispatch.run_git(wt, "rev-parse", "HEAD").strip()
            if (verifies.__setitem__("n", verifies["n"] + 1) or
                verifies["n"] >= 3) else "stale"))
    sleeps: list[float] = []
    monkeypatch.setattr(e2e_dispatch.time, "sleep",
                        lambda s: sleeps.append(s))
    sha = e2e_dispatch.push_signal_branch(
        repo, "main2main_e2e", "fork/x", [], 1, "7")
    assert pushes["n"] == 3 and verifies["n"] == 3
    assert sha


def test_push_signal_branch_raises_after_exhaustion(tmp_path: Path,
                                                    monkeypatch) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)
    monkeypatch.setattr(
        e2e_dispatch, "_push_via_proxy",
        lambda wt, fork, refspec, *a: None)
    monkeypatch.setattr(e2e_dispatch, "_remote_branch_sha",
                        lambda wt, fork, branch: "stale")
    monkeypatch.setattr(e2e_dispatch.time, "sleep", lambda s: None)
    with pytest.raises(RuntimeError, match="failed after 5 attempts"):
        e2e_dispatch.push_signal_branch(
            repo, "main2main_e2e", "fork/x", [], 1, "7")


def test_run_external_e2e_push_failure_structured(tmp_path: Path,
                                                  monkeypatch) -> None:
    # A signal push that fails after internal retries must degrade to a
    # structured failed result, not crash the flow process.
    cfg = e2e_dispatch.E2EDispatchConfig(signal_branch="main2main_e2e",
                                         signal_repo="fork/x",
                                         main_run_id="42")
    def boom(*a, **k):
        raise RuntimeError("channel down")
    monkeypatch.setattr(e2e_dispatch, "push_signal_branch", boom)
    result = e2e_dispatch.run_external_e2e(
        cfg, tmp_path / "ascend",
        [{"npu_type": "a2", "tests": "tests/e2e/a2/test_x.py"}],
        tmp_path / "log", 1, step_id=0)
    assert result["ci_result"] == "failed"
    assert result["can_commit"] is False
    assert "signal branch push failed" in result["summary_error"]


def _fake_clock():
    clock = SimpleNamespace(t=1000.0)
    clock.time = lambda: clock.t
    clock.sleep = lambda s: setattr(clock, "t", clock.t + s)
    return clock


def test_wait_chip_results_all_report(tmp_path: Path, monkeypatch) -> None:
    cfg = e2e_dispatch.E2EDispatchConfig(signal_branch="main2main_e2e",
                                         signal_repo="fork/x")
    fetched: list[tuple[str, int]] = []

    def fake_fetch(git_url, branch, round_number, dest, expect=None):
        fetched.append((branch, round_number))
        dest.mkdir(parents=True, exist_ok=True)
        return True

    monkeypatch.setattr(e2e_dispatch, "_fetch_round_results", fake_fetch)
    clock = _fake_clock()
    monkeypatch.setattr(e2e_dispatch, "time", clock)
    missing = e2e_dispatch.wait_chip_results(
        cfg, ["a2", "310p"], 2, 10, tmp_path, command_sha="abc123")
    assert missing == []
    assert set(fetched) == {("main2main_e2e_results_a2", 2),
                            ("main2main_e2e_results_310p", 2)}
    for chip in ("a2", "310p"):
        assert (tmp_path / f"main2main-e2e-round-2-{chip}").is_dir()


def test_wait_chip_results_slow_chip_then_timeout(tmp_path: Path,
                                                  monkeypatch) -> None:
    cfg = e2e_dispatch.E2EDispatchConfig(signal_branch="main2main_e2e",
                                         signal_repo="fork/x")
    polls = {"a2": 0, "310p": 0}

    def fake_fetch(git_url, branch, round_number, dest, expect=None):
        chip = branch.rsplit("_", 1)[-1]
        polls[chip] += 1
        if chip == "a2" and polls[chip] >= 2:
            dest.mkdir(parents=True, exist_ok=True)
            return True
        return False

    monkeypatch.setattr(e2e_dispatch, "_fetch_round_results", fake_fetch)
    clock = _fake_clock()
    monkeypatch.setattr(e2e_dispatch, "time", clock)
    # Budget: 2 polls worth (2 x 30s sleep) exhausts it before 310p reports.
    missing = e2e_dispatch.wait_chip_results(
        cfg, ["a2", "310p"], 1, 1, tmp_path)
    assert missing == ["310p"]
    assert (tmp_path / "main2main-e2e-round-1-a2").is_dir()
    assert not (tmp_path / "main2main-e2e-round-1-310p").exists()


def test_run_external_e2e_timeout_returns_failed_result(
        tmp_path: Path, monkeypatch) -> None:
    cfg = e2e_dispatch.E2EDispatchConfig(signal_branch="main2main_e2e",
                                         signal_repo="fork/x",
                                         main_run_id="42")
    monkeypatch.setattr(e2e_dispatch, "push_signal_branch",
                        lambda *a, **k: "deadbeef")
    seen_expect: list = []

    def fake_fetch(git_url, branch, round_number, dest, expect=None):
        seen_expect.append(expect)
        return False

    monkeypatch.setattr(e2e_dispatch, "_fetch_round_results", fake_fetch)
    clock = _fake_clock()
    monkeypatch.setattr(e2e_dispatch, "time", clock)
    result = e2e_dispatch.run_external_e2e(
        cfg, tmp_path / "ascend",
        [{"npu_type": "a2", "tests": "tests/e2e/a2/test_x.py"}],
        tmp_path / "log", 1, step_id=0, timeout_min=1)
    assert result["ci_result"] == "failed"
    assert result["can_commit"] is False
    assert "no results from chips ['a2']" in result["summary_error"]
    # The pushed command sha must be bound into the results identity check.
    assert seen_expect[-1] == {"main_run_id": "42", "command_sha": "deadbeef"}


def test_run_external_e2e_no_resident_chips_passes(
        tmp_path: Path, monkeypatch) -> None:
    # Groups only for chips without resident jobs (e.g. a3 while it is out
    # of the matrix) must not block on the timeout — nothing to run.
    cfg = e2e_dispatch.E2EDispatchConfig(signal_branch="main2main_e2e",
                                         signal_repo="fork/x",
                                         main_run_id="42")
    monkeypatch.setenv("MAIN2MAIN_E2E_CHIPS", "a2,310p")
    monkeypatch.setattr(e2e_dispatch, "push_signal_branch",
                        lambda *a, **k: "deadbeef")
    result = e2e_dispatch.run_external_e2e(
        cfg, tmp_path / "ascend",
        [{"npu_type": "a3", "tests": "tests/e2e/a3_4/test_y.py"}],
        tmp_path / "log", 1, step_id=0)
    assert result["ci_result"] == "passed"
    assert result["can_commit"] is True


def _init_origin_with_round(tmp_path: Path, main_run_id: str,
                            command_sha: str) -> Path:
    """Bare-ish local origin holding one chip results branch (round-1/)."""
    origin = tmp_path / "origin"
    origin.mkdir()
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(origin), check=True,
                       capture_output=True, text=True)
    git("init", "-q")
    git("checkout", "-q", "-b", "main2main_e2e_results_a2")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (origin / "round-1").mkdir()
    (origin / "round-1" / "round-meta.json").write_text(json.dumps(
        {"round": 1, "main_run_id": main_run_id,
         "command_sha": command_sha}), encoding="utf-8")
    (origin / "round-1" / "round-1-result.json").write_text(
        "{}", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "results")
    return origin


def test_fetch_round_results_identity_enforced(tmp_path: Path) -> None:
    origin = _init_origin_with_round(tmp_path, "42", "cafe123")
    dest = tmp_path / "out"
    ok = e2e_dispatch._fetch_round_results(
        str(origin), "main2main_e2e_results_a2", 1, dest,
        expect={"main_run_id": "42", "command_sha": "cafe123"})
    assert ok is True
    assert (dest / "round-1-result.json").exists()
    assert (dest / "round-meta.json").exists()
    # Stale results from a different command sha / main run are rejected.
    dest2 = tmp_path / "out2"
    assert e2e_dispatch._fetch_round_results(
        str(origin), "main2main_e2e_results_a2", 1, dest2,
        expect={"main_run_id": "42", "command_sha": "other"}) is False
    dest3 = tmp_path / "out3"
    assert e2e_dispatch._fetch_round_results(
        str(origin), "main2main_e2e_results_a2", 1, dest3,
        expect={"main_run_id": "99", "command_sha": "cafe123"}) is False
    # No expect: legacy behavior, accept whatever is there.
    dest4 = tmp_path / "out4"
    assert e2e_dispatch._fetch_round_results(
        str(origin), "main2main_e2e_results_a2", 1, dest4) is True
    # Re-materializing the same dest must not overlay stale content.
    (dest / "round-1-stale.log").write_text("stale", encoding="utf-8")
    e2e_dispatch._fetch_round_results(
        str(origin), "main2main_e2e_results_a2", 1, dest,
        expect={"main_run_id": "42", "command_sha": "cafe123"})
    assert not (dest / "round-1-stale.log").exists()
