"""Pair-aligned device assignment for dual-die NPUs (A3) + OOM-hang env-flake
classification."""
from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

from main2main_flow.scripts.utils import run_tests as rt
from main2main_flow.scripts.utils.run_tests import (
    _apply_device_constraints,
    _assign_devices,
    _is_oom_env_failure,
    _is_psutil_cleanup_race,
    _pair_complete_pool,
    _run_one_test,
    _schedule_rounds,
    _test_cards,
    _validate_pair_aligned,
)

def _make_round(*counts):
    """One round of tests whose paths encode the given card counts."""
    slugs = {
        1: "one_card", 2: "two_card", 4: "four_card", 8: "eight_card",
    }
    return [[f"tests/e2e/pull_request/{slugs[c]}/test_{i}.py"
             for i, c in enumerate(counts)]]


def _make_tests(*counts):
    """Flat test list with unique paths encoding the given card counts."""
    slugs = {
        1: "one_card", 2: "two_card", 4: "four_card", 8: "eight_card",
    }
    return [f"tests/e2e/pull_request/{slugs[c]}/test_{i}.py"
            for i, c in enumerate(counts)]


def _devices(rnd):
    return [d for _, d in rnd]


# ---- _validate_pair_aligned -------------------------------------------------

def test_validation_accepts_complete_pairs():
    _validate_pair_aligned([0, 1, 2, 3, 4, 5])
    _validate_pair_aligned([2, 3, 4, 5])  # pod starting at physical 2
    _validate_pair_aligned([0, 1, 6, 7])  # gaps still fine as long as pairs close


def test_validation_rejects_lone_dies():
    with pytest.raises(ValueError, match="lone"):
        _validate_pair_aligned([1, 2, 3])  # die 1's partner 0 missing
    with pytest.raises(ValueError, match="lone"):
        _validate_pair_aligned([0, 1, 3, 4, 5])  # 3 split from 2
    with pytest.raises(ValueError, match="lone"):
        _validate_pair_aligned([0, 2, 3])  # 0's partner 1 missing


# ---- _assign_devices (pair-aligned) -----------------------------------------

def test_pair_aligned_even_test_gets_complete_pairs():
    rnd = _make_round(2, 2)  # two two_card tests
    out = _assign_devices(rnd, [0, 1, 2, 3], pair_aligned=True)
    assert _devices(out[0]) == ["0,1", "2,3"]


def test_pair_aligned_one_card_never_takes_odd_die():
    rnd = _make_round(1, 1)  # two one_card tests
    out = _assign_devices(rnd, [0, 1, 2, 3], pair_aligned=True)
    assert _devices(out[0]) == ["0", "2"]  # die 1 left unused, not shared


def test_pair_aligned_odd_offset_skipped():
    # one_card then two_card: the two_card must not start on odd die 1
    rnd = _make_round(1, 2)
    out = _assign_devices(rnd, [0, 1, 2, 3], pair_aligned=True)
    assert _devices(out[0]) == ["0", "2,3"]


def test_pair_aligned_four_card_matches_hardcoded_range():
    rnd = _make_round(4)
    out = _assign_devices(rnd, [0, 1, 2, 3, 4, 5], pair_aligned=True)
    assert _devices(out[0]) == ["0,1,2,3"]


def test_pair_aligned_pod_starting_at_even_physical_id():
    # pod allocated physical 2-5: logical ids mirror them, still paired
    rnd = _make_round(2, 2)
    out = _assign_devices(rnd, [2, 3, 4, 5], pair_aligned=True)
    assert _devices(out[0]) == ["2,3", "4,5"]


def test_pair_aligned_rejects_broken_pod_allocation():
    rnd = _make_round(2)
    with pytest.raises(ValueError, match="lone"):
        _assign_devices(rnd, [1, 3], pair_aligned=True)


def test_non_pair_aligned_behavior_unchanged():
    # default path keeps the historical sequential packing
    rnd = _make_round(1, 1)
    out = _assign_devices(rnd, [0, 1, 2, 3], pair_aligned=False)
    assert _devices(out[0]) == ["0", "1"]


def test_multiple_rounds_each_restart_from_zero():
    rounds = [_make_round(2)[0], _make_round(1)[0]]
    out = _assign_devices(rounds, [0, 1, 2, 3], pair_aligned=True)
    assert _devices(out[0]) == ["0,1"]
    assert _devices(out[1]) == ["0"]


def test_cards_inferred_from_path():
    assert _test_cards("tests/e2e/pull_request/one_card/test_x.py") == 1
    assert _test_cards("tests/e2e/pull_request/two_card/test_x.py") == 2
    assert _test_cards("tests/e2e/pull_request/four_card/test_x.py") == 4


# ---- partial-occupancy degradation (run 33974526604: chips 8-15 occupied) ----

def test_pair_complete_pool_filters_lone_dies():
    # scattered occupancy leaves a lone die between complete pairs
    assert _pair_complete_pool([0, 1, 2, 4, 5]) == ([0, 1, 4, 5], [2])
    assert _pair_complete_pool([0, 1, 2, 3]) == ([0, 1, 2, 3], [])


def test_pair_complete_pool_all_lone():
    assert _pair_complete_pool([0, 2, 4]) == ([], [0, 2, 4])


def test_apply_constraints_skips_over_capacity_tests():
    # one dual-die pair survived (capacity 2): four_card cannot run
    tests = ["tests/e2e/pull_request/one_card/test_a.py",
             "tests/e2e/pull_request/four_card/test_b.py"]
    runnable, skipped = _apply_device_constraints(tests, 2, 0)
    assert runnable == [tests[0]]
    assert skipped == [{"test": tests[1], "cards": 4,
                        "reason": "needs 4 cards, only 2 usable"}]


def test_apply_constraints_skips_overriders_when_pool_not_from_zero():
    # overriders hardcode physical 0..N-1 for their child servers — with the
    # pool starting at 8 those ids point at occupied chips
    tests = ["tests/e2e/pull_request/two_card/test_disaggregated_encoder.py",
             "tests/e2e/pull_request/one_card/test_a.py"]
    overriders = {tests[0]}
    runnable, skipped = _apply_device_constraints(tests, 2, 8, overriders)
    assert runnable == [tests[1]]
    assert skipped[0]["test"] == tests[0]
    assert "starts at 8" in skipped[0]["reason"]
    # pool starting at 0 keeps the overrider
    runnable, skipped = _apply_device_constraints(tests, 2, 0, overriders)
    assert runnable == tests
    assert skipped == []


def test_assign_devices_on_filtered_pool():
    # pool [0,1,4,5] (chips 2-3 busy): fills from the front — one_card takes
    # die 0, then the parity skip pushes the 2-card test onto pair (4,5)
    out = _assign_devices(_make_round(1, 2), [0, 1, 4, 5], pair_aligned=True)
    assert _devices(out[0]) == ["0", "4,5"]
    out = _assign_devices(_make_round(2), [0, 1, 4, 5], pair_aligned=True)
    assert _devices(out[0]) == ["0,1"]


# ---- NPU-OOM hang → env_flake_pass (runs 33314256232/33406387872) ------------

OOM_WORKER_LINE = (
    "ERROR 09-01 10:00:00 [multiproc_executor.py:521] Worker failed with "
    "error 'NPU out of memory. Tried to allocate 9.98 GiB (NPU 0; 61.28 GiB "
    "total; 55.17 GiB already allocated; 2.76 GiB free)'")
SKIP_GATHER_WORKER_LINE = (
    "ERROR 09-01 10:00:00 [multiproc_executor.py:521] Worker failed with "
    "error 'TypeError: AscendLogitsProcessor._get_logits() takes from 3 to 4 "
    "positional arguments but 5 were given'")


def _write_log(tmp_path: Path, body: str, name: str = "test.log") -> Path:
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_oom_env_pure_oom_classified(tmp_path: Path):
    # Round-2/3 gemma4 shape: OOM worker death + a bare traceback SOURCE line
    # ("RuntimeError(" has no message) + pytest killed by the suite timeout.
    log = _write_log(tmp_path, "\n".join([
        "INFO ... adding tasks",
        OOM_WORKER_LINE,
        'raise RuntimeError(',
        "[TIMEOUT] suite exceeded 1800s, killing process group",
    ]) + "\n")
    assert _is_oom_env_failure(log)


def test_oom_env_ansi_colored_real_error_not_classified(tmp_path: Path):
    # ANSI codes around AssertionError must not hide a real failure
    # (prefix_caching garbage output asserts with colored pytest output).
    log = _write_log(tmp_path, "\n".join([
        OOM_WORKER_LINE,
        "E   \x1b[31mAssertionError\x1b[0m: Test0: vllm_output does not "
        "match golden",
    ]) + "\n")
    assert not _is_oom_env_failure(log)


def test_oom_env_non_oom_worker_error_not_classified(tmp_path: Path):
    # Round-1 gemma4 shape: engine died of the skip_gather TypeError —
    # a real adaptation bug, even though OOM text may appear elsewhere.
    log = _write_log(tmp_path, "\n".join([
        SKIP_GATHER_WORKER_LINE,
        "NPU out of memory is mentioned in a doc line",
    ]) + "\n")
    assert not _is_oom_env_failure(log)


def test_oom_env_mixed_worker_errors_not_classified(tmp_path: Path):
    log = _write_log(tmp_path, "\n".join([
        OOM_WORKER_LINE,
        "ERROR Worker failed with error 'AssertionError: Engine core "
        "initialization failed'",
    ]) + "\n")
    assert not _is_oom_env_failure(log)


def test_oom_env_no_oom_signature_not_classified(tmp_path: Path):
    log = _write_log(tmp_path, "E   AssertionError: garbage output\n")
    assert not _is_oom_env_failure(log)


# ---- run 38512795089 shapes (EngineDeadError-wrapped OOM, psutil race) -------

ENGINEDEAD_LINE = (
    "E           vllm.v1.engine.exceptions.EngineDeadError: EngineCore "
    "encountered an issue. See stack trace (above) for the root cause.")


def test_oom_env_enginedead_wrapper_classified(tmp_path: Path):
    # test_vlm/test_whisper shape (run 38512795089): OOM inside the worker
    # stack, death re-raised to the test as EngineDeadError — pytest exits 1,
    # not -9, and the wrapper used to block the OOM classification.
    log = _write_log(tmp_path, "\n".join([
        "(EngineCore pid=1) ERROR [core.py:1438] torch.OutOfMemoryError: "
        "NPU out of memory. Tried to allocate 6.75 GiB",
        ENGINEDEAD_LINE,
        "FAILED tests/e2e/pull_request/one_card/test_vlm.py::test_whisper",
    ]) + "\n")
    assert _is_oom_env_failure(log)


def test_oom_env_engine_init_wrapper_classified(tmp_path: Path):
    # test_sampler shape (run 38512795089): OOM during NPUModelRunner init,
    # "Engine core initialization failed" re-raise + EngineDeadError, suite
    # hung to the timeout SIGKILL.
    log = _write_log(tmp_path, "\n".join([
        OOM_WORKER_LINE,
        "RuntimeError: Engine core initialization failed. See root cause "
        "above. Failed core proc(s): {}",
        ENGINEDEAD_LINE,
        "[TIMEOUT] suite exceeded 1800s, killing process group",
    ]) + "\n")
    assert _is_oom_env_failure(log)


def test_run_one_test_oom_becomes_env_flake(monkeypatch, tmp_path: Path):
    # exit -9 (suite-timeout SIGKILL) + pure-OOM log → env_flake_pass, so the
    # round doesn't count as an adaptation failure.
    log = _write_log(tmp_path, OOM_WORKER_LINE + "\n", "oom.log")
    summary = _write_log(tmp_path, "", "oom-summary.json")
    monkeypatch.setattr(rt, "_run_to_log", lambda *a, **k: -9)
    monkeypatch.setattr(rt, "_run_summary",
                        lambda *a, **k: {"summary": {"code_bugs": [],
                                                     "env_flakes": []},
                                         "summary_error": None})
    result = _run_one_test(
        ["pytest"], log, summary, "tests/e2e/pull_request/two_card/test_g.py",
        "0,1", tmp_path / "ci", tmp_path, 0, 1, {},
        is_remote=False, is_mock=False)
    assert result["ci_result"] == "env_flake_pass"
    assert result["run_suite_exit_code"] == -9


def test_run_one_test_oom_with_real_error_stays_failed(monkeypatch,
                                                       tmp_path: Path):
    log = _write_log(tmp_path, "\n".join([
        OOM_WORKER_LINE,
        "E   AssertionError: vllm_output does not match golden",
    ]) + "\n", "real.log")
    summary = _write_log(tmp_path, "", "real-summary.json")
    monkeypatch.setattr(rt, "_run_to_log", lambda *a, **k: -9)
    monkeypatch.setattr(rt, "_run_summary",
                        lambda *a, **k: {"summary": {"code_bugs": [],
                                                     "env_flakes": []},
                                         "summary_error": None})
    result = _run_one_test(
        ["pytest"], log, summary, "tests/e2e/pull_request/two_card/test_g.py",
        "0,1", tmp_path / "ci", tmp_path, 0, 1, {},
        is_remote=False, is_mock=False)
    assert result["ci_result"] == "failed"


def test_run_one_test_enginedead_oom_exit1_becomes_env_flake(monkeypatch,
                                                             tmp_path: Path):
    # test_vlm/test_whisper (run 38512795089): OOM death re-raised as
    # EngineDeadError fails pytest with exit 1 — the widened OOM gate must
    # env-flake it too, not just the -9 hang shape.
    log = _write_log(tmp_path, "\n".join([
        "(EngineCore pid=1) ERROR [core.py:1438] torch.OutOfMemoryError: "
        "NPU out of memory. Tried to allocate 6.75 GiB",
        ENGINEDEAD_LINE,
        "FAILED tests/e2e/pull_request/one_card/test_vlm.py::test_whisper",
    ]) + "\n", "whisper.log")
    summary = _write_log(tmp_path, "", "whisper-summary.json")
    monkeypatch.setattr(rt, "_run_to_log", lambda *a, **k: 1)
    monkeypatch.setattr(rt, "_run_summary",
                        lambda *a, **k: {"summary": {"code_bugs": [],
                                                     "env_flakes": []},
                                         "summary_error": None})
    result = _run_one_test(
        ["pytest"], log, summary,
        "tests/e2e/pull_request/one_card/test_vlm.py",
        "0", tmp_path / "ci", tmp_path, 0, 1, {},
        is_remote=False, is_mock=False)
    assert result["ci_result"] == "env_flake_pass"
    assert result["run_suite_exit_code"] == 1


def test_psutil_cleanup_race_classified(tmp_path: Path):
    # two_card/test_pipeline_parallel shape (run 38512795089): the engine
    # child died, then the test's psutil enumeration raced the vanished pid.
    log = _write_log(tmp_path, "\n".join([
        "self = <psutil._pslinux.Process object>",
        "E               psutil.NoSuchProcess: process no longer exists "
        "(pid=617773)",
        "/usr/local/python3.12.13/lib/python3.12/site-packages/psutil/"
        "_pslinux.py:1726: NoSuchProcess",
    ]) + "\n")
    assert _is_psutil_cleanup_race(log)


def test_psutil_cleanup_race_with_real_error_not_classified(tmp_path: Path):
    # A psutil exception next to a real raised error never env-flakes: a
    # genuine engine-killing code bug leaves its own traceback in the log.
    log = _write_log(tmp_path, "\n".join([
        "E   AssertionError: vllm_output does not match golden",
        "E               psutil.NoSuchProcess: process no longer exists "
        "(pid=617773)",
    ]) + "\n")
    assert not _is_psutil_cleanup_race(log)


def test_psutil_cleanup_race_no_psutil_not_classified(tmp_path: Path):
    log = _write_log(tmp_path, "E   RuntimeError: garbage output\n")
    assert not _is_psutil_cleanup_race(log)


def test_run_one_test_psutil_race_becomes_env_flake(monkeypatch,
                                                    tmp_path: Path):
    log = _write_log(tmp_path, "E   psutil.NoSuchProcess: process no longer "
                     "exists (pid=617773)\n", "psutil.log")
    summary = _write_log(tmp_path, "", "psutil-summary.json")
    monkeypatch.setattr(rt, "_run_to_log", lambda *a, **k: 1)
    monkeypatch.setattr(rt, "_run_summary",
                        lambda *a, **k: {"summary": {"code_bugs": [],
                                                     "env_flakes": []},
                                         "summary_error": None})
    result = _run_one_test(
        ["pytest"], log, summary,
        "tests/e2e/pull_request/two_card/test_pipeline_parallel.py",
        "0,1", tmp_path / "ci", tmp_path, 0, 1, {},
        is_remote=False, is_mock=False)
    assert result["ci_result"] == "env_flake_pass"
    assert result["run_suite_exit_code"] == 1


# ---- numeric-precision failures → precision_pass (2026-09-03 user) ----

def test_precision_failure_detected(tmp_path: Path) -> None:
    log = tmp_path / "t.log"
    log.write_text(
        ">           assert torch.allclose(hf_output, vllm_output, 1e-2)\n"
        "E           assert False\n"
        "E            +  where False = allclose(tensor([0.0018, 0.9980]), "
        "tensor([0.0018, 0.9982]), 0.01)\n"
        "tests/.../test_classification_310p.py:39: AssertionError\n",
        encoding="utf-8")
    assert rt._is_precision_failure(log)


def test_precision_failure_not_detected_for_other_assert(tmp_path: Path) -> None:
    log = tmp_path / "t.log"
    log.write_text(
        "E   AssertionError: pipeline model parallel group is not initialized\n",
        encoding="utf-8")
    assert not rt._is_precision_failure(log)


def test_precision_failure_not_detected_for_tp_size_mismatch(tmp_path: Path) -> None:
    # Config mismatch, not numeric drift: "are not equal" alone (no
    # allclose/assert_close/mismatch-diff signature) must NOT classify as
    # precision_pass — that would mask a broken test as trackable-pass.
    log = tmp_path / "t.log"
    log.write_text(
        "E   AssertionError: TP size (8) and NPU count (4) are not equal\n",
        encoding="utf-8")
    assert not rt._is_precision_failure(log)


def test_precision_failure_detected_for_graph_mode_logprob(tmp_path: Path) -> None:
    # run 33897770317: four_card/test_graph_mode.py aclgraph baseline-vs-
    # compiled decode logprob assertion (own decode_atol) on the a3 soc.
    log = tmp_path / "t.log"
    log.write_text(
        "E   AssertionError: Decode logprob mismatch at prompt 2, token 3: "
        "baseline=-1.6962, compiled=-1.0638, diff=0.6324 > decode_atol=0.1378\n",
        encoding="utf-8")
    assert rt._is_precision_failure(log)


def test_npu_memory_pressure_detected(tmp_path: Path) -> None:
    # run 33897770317: one_card/test_sampler.py — harness-level resource
    # check (tests/e2e/conftest.py) hit while sibling suites held HBM.
    log = tmp_path / "t.log"
    log.write_text(
        "E               RuntimeError: Failed to get enough NPU memory! "
        "Available: 3.12 GiB, Required: 55.14 GiB.\n"
        "tests/e2e/conftest.py:1290: RuntimeError\n",
        encoding="utf-8")
    assert rt._is_npu_memory_pressure(log)
    other = tmp_path / "ok.log"
    other.write_text("E   AssertionError: sampler params invalid\n",
                     encoding="utf-8")
    assert not rt._is_npu_memory_pressure(other)


def test_aggregate_precision_pass_can_commit(tmp_path: Path) -> None:
    from main2main_flow.scripts.utils.run_tests import aggregate_suite_results
    r = aggregate_suite_results(
        0, 1, [{"test": "t1", "ci_result": "passed", "code_bugs_count": 0,
                "env_flakes_count": 0, "failed_test_files_count": 0,
                "failed_test_cases_count": 0},
               {"test": "t2", "ci_result": "precision_pass",
                "code_bugs_count": 0, "env_flakes_count": 0,
                "failed_test_files_count": 1, "failed_test_cases_count": 1}],
        total_cards=1, sequential=False, remote="x", ci_dir=tmp_path,
        rounds_info=[], total_elapsed=1.0)
    assert r["ci_result"] == "precision_pass"
    assert r["can_commit"] is True
    assert r["requires_fix"] is False

# ---- run 34018086282: timeout-kill with no observed failure → env_flake ------
# deepseek_pruning round-0 was killed at the 3600s suite timeout (exit=-9)
# with zero failed cases and no error in its log — an environment stall, not
# an adaptation bug — yet it was classified "failed" and burned 2 adapter
# fix rounds (~2.5h).

def test_no_observed_failure_clean_log(tmp_path: Path):
    log = _write_log(tmp_path, "INFO engine started\ntest_a PASSED\n")
    assert rt._no_observed_failure({"failed_test_cases": 0,
                                    "failed_test_files": []}, log)


def test_no_observed_failure_summary_has_failures(tmp_path: Path):
    log = _write_log(tmp_path, "something\n")
    assert not rt._no_observed_failure({"failed_test_cases": ["c1", "c2"],
                                        "failed_test_files": []}, log)
    assert not rt._no_observed_failure({"failed_test_cases_count": 2,
                                        "failed_test_files_count": 0}, log)


def test_no_observed_failure_missing_log(tmp_path: Path):
    assert not rt._no_observed_failure({"failed_test_cases": [],
                                        "failed_test_files": 0},
                                       tmp_path / "absent.log")


def test_no_observed_failure_raised_error_in_log(tmp_path: Path):
    log = _write_log(tmp_path, "Traceback:\nRuntimeError: engine died\n")
    assert not rt._no_observed_failure({"failed_test_cases": [],
                                        "failed_test_files": []}, log)


def test_no_observed_failure_worker_wrapper_stripped(tmp_path: Path):
    # OOM text only appears inside a "Worker failed with error" wrapper line
    # (the executor's aggregate message); stripping those must not un-hide a
    # genuine raise, but a wrapper-only log means nothing actually failed.
    wrapper_only = _write_log(tmp_path, OOM_WORKER_LINE + "\n", "w.log")
    assert rt._no_observed_failure({"failed_test_cases": 0,
                                    "failed_test_files": 0}, wrapper_only)
    real = _write_log(tmp_path, OOM_WORKER_LINE +
                      "\nValueError: bad config\n", "r.log")
    assert not rt._no_observed_failure({"failed_test_cases": [],
                                        "failed_test_files": 0}, real)


def test_run_one_test_timeout_kill_no_failure_becomes_env_flake(
        monkeypatch, tmp_path: Path):
    # deepseek_pruning shape: SIGKILLed at the suite timeout, summary shows
    # zero failures, log shows no error — environment stall, not adaptation.
    log = _write_log(tmp_path, "INFO adding tasks\n", "pruning.log")
    summary = _write_log(tmp_path, "", "pruning-summary.json")
    monkeypatch.setattr(rt, "_run_to_log", lambda *a, **k: -9)
    monkeypatch.setattr(rt, "_run_summary",
                        lambda *a, **k: {"summary": {"code_bugs": [],
                                                     "env_flakes": []},
                                         "summary_error": None})
    result = _run_one_test(
        ["pytest"], log, summary,
        "tests/e2e/pull_request/two_card/test_pruning.py", "0,1",
        tmp_path / "ci", tmp_path, 0, 1, {},
        is_remote=False, is_mock=False)
    assert result["ci_result"] == "env_flake_pass"


# ---- run 34018086282: hang early-kill (MAIN2MAIN_HANG_QUIET_S) ---------------
# gemma4's memory-wait hang streamed nothing for hours while the suite
# timeout sat at 3600s — three e2e rounds each paid the full hour (~3h).

def test_run_to_log_hang_quiet_kill(tmp_path: Path, monkeypatch):
    # subprocess prints one line, then goes silent: quiet-kill must fire
    # well before the (never-reached) 3600s deadline.
    monkeypatch.setenv("MAIN2MAIN_HANG_QUIET_S", "2")
    log = tmp_path / "hang.log"
    cmd = [sys.executable, "-c",
           "print('started', flush=True); import time; time.sleep(300)"]
    t0 = time.monotonic()
    code = rt._run_to_log(cmd, tmp_path, log, {}, timeout_s=3600)
    elapsed = time.monotonic() - t0
    assert code == -9
    assert elapsed < 30
    assert "started" in log.read_text()


def test_run_to_log_active_output_not_killed(tmp_path: Path, monkeypatch):
    # output arriving more often than the quiet window keeps it alive
    monkeypatch.setenv("MAIN2MAIN_HANG_QUIET_S", "2")
    log = tmp_path / "alive.log"
    cmd = [sys.executable, "-c",
           "import time\n"
           "for _ in range(5):\n"
           "    print('tick', flush=True)\n"
           "    time.sleep(1)\n"
           "print('done', flush=True)"]
    code = rt._run_to_log(cmd, tmp_path, log, {}, timeout_s=60)
    assert code == 0
    assert "done" in log.read_text()


# ---- run 34010715527: pair-aligned scheduling overflow ----------------------
# _schedule_rounds packed rounds by raw card count while _assign_devices
# bumps the offset to the next pair after every odd-need test, so a round of
# exactly [4c,2c,1c,1c] (8 raw cards) overflowed the 8-die pool on the
# trailing alignment bump -> IndexError killed the whole flow.

def test_schedule_rounds_pair_aligned_charges_even_costs():
    tests = _make_tests(4, 2, 1, 1)
    rounds = _schedule_rounds(tests, 8, pair_aligned=True)
    for rnd in rounds:
        cost = sum(_test_cards(t) + _test_cards(t) % 2 for t in rnd)
        assert cost <= 8
    # the tight-but-overflowing round must not appear
    assert [sorted(_test_cards(t) for t in r) for r in rounds] \
        != [[1, 1, 2, 4]]
    # raw packing (pair_aligned=False) still produces it — behavior change
    # is scoped to dual-die runners
    assert [[sorted(_test_cards(t) for t in r) for r in
             _schedule_rounds(tests, 8, pair_aligned=False)][0]] \
        == [[1, 1, 2, 4]]


def test_schedule_rounds_assign_end_to_end_pool_offset8():
    # full run 34010715527 shape: 10 runnable tests on pool [8..15]
    tests = _make_tests(4, 4, 4, 2, 2, 2, 1, 1, 1, 1)
    rounds = _schedule_rounds(tests, 8, pair_aligned=True)
    out = _assign_devices(rounds, [8, 9, 10, 11, 12, 13, 14, 15],
                          pair_aligned=True)
    assert len(out) == len(rounds)
    pool = {"8", "9", "10", "11", "12", "13", "14", "15"}
    for rnd in out:
        for _, devs in rnd:
            assert set(devs.split(",")) <= pool


# ---- diff-driven test selection is retired (2026-09-13) ---------------------
# PR #16439: upstream vllm 07b75534 added a kwarg to the SimpleCPUOffload
# coordinator call while vllm-ascend's existing patch wrapper wasn't
# extended — the engine died at connector init, and no diff-driven
# selector could ever have caught it: the PR diff doesn't touch the
# ascend patch, and upstream removed select_tests.py's --changed-files
# mode on 09-01 (#15447) anyway.  The fixed test-policy set is the only
# selection source now.

def test_diff_driven_selection_is_retired(tmp_path, monkeypatch):
    # select_by_files is accepted for call-site compatibility but must not
    # trigger any subprocess (no select_tests.py invocation), and with no
    # test_cases the run reports "no tests requested" — never a selection.
    def _boom(*a, **k):
        raise AssertionError("subprocess called during test resolution")
    monkeypatch.setattr(rt.subprocess, "run", _boom)
    r = rt.run_tests(
        vllm_path=tmp_path, vllm_commit="0" * 40,
        ascend_path=tmp_path, ascend_commit="1" * 40,
        step_id=9, select_by_files=["vllm_ascend/foo.py"])
    assert r["can_commit"] is True
    assert r["suite_results"] == {}


def test_selection_error_still_importable():
    # legacy import compat for the retired mechanism's error type
    assert issubclass(rt.TestSelectionError, RuntimeError)


# ---- missing selected cases must be dropped loudly, not wedge the run -----
# Run 34745454795: two phantom policy cases (files don't exist on upstream
# main) failed pytest collection — exit 4 is unfixable for the adapter, the
# e2e blocking set froze, stop-loss reverted the tree, no PR.

def test_drop_missing_cases(tmp_path: Path) -> None:
    (tmp_path / "tests" / "e2e" / "one_card").mkdir(parents=True)
    real = "tests/e2e/one_card/test_real.py"
    (tmp_path / real).write_text("def test_method():\n    pass\n")
    node = real + "::TestClass::test_method"
    phantom = "tests/e2e/one_card/test_phantom.py"
    kept = rt._drop_missing_cases([real, node, phantom], tmp_path)
    assert kept == [real, node]


def test_drop_missing_cases_reports_and_keeps_existing(tmp_path: Path, capsys) -> None:
    phantom = "tests/e2e/one_card/test_phantom.py"
    assert rt._drop_missing_cases([phantom], tmp_path) == []
    assert "test_phantom.py" in capsys.readouterr().out


# ---- case-level drop: file exists but the selected function doesn't -------
# Run 38220121641: test_eagle3_sliding_window — the file survived an upstream
# rename, the selected function didn't; file-level check passed it, pytest
# exit-4'd, and the step wedged (the adapter cannot add tests).

def test_drop_missing_cases_function_missing_dropped(
        tmp_path: Path, capsys) -> None:
    rel = "tests/e2e/one_card/test_spec_decode.py"
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text("def test_eagle3_basic():\n    pass\n")
    case = rel + "::test_eagle3_sliding_window"
    assert rt._drop_missing_cases([case], tmp_path) == []
    assert "test_eagle3_sliding_window" in capsys.readouterr().out


def test_drop_missing_cases_parametrized_present_kept(tmp_path: Path) -> None:
    rel = "tests/e2e/one_card/test_pipeline_parallel.py"
    (tmp_path / rel).parent.mkdir(parents=True)
    (tmp_path / rel).write_text(
        "def test_models_pp2_dp2(mode, tp, dp, model):\n    pass\n")
    case = rel + "::test_models_pp2_dp2[mp-2-2-deepseek-ai/DeepSeek-V2-Lite-Chat]"
    assert rt._drop_missing_cases([case], tmp_path) == [case]


def test_drop_missing_cases_unreadable_file_fails_open(tmp_path: Path) -> None:
    # Unreadable file: keep the case and let pytest surface the problem.
    rel = "tests/e2e/one_card/test_x.py"
    p = tmp_path / rel
    p.parent.mkdir(parents=True)
    p.mkdir()  # a directory where the test file should be → read_text fails
    case = rel + "::test_y"
    assert rt._drop_missing_cases([case], tmp_path) == [case]


# ---- download env-flake: terminal-error-aware classification ---------------
# Run 38220121641: the modelscope download failure itself arrives wrapped in
# a full traceback (server/DP-worker children tee their output into the test
# log), so the old "traceback + exception name" guard refused to classify
# pure download failures — three server-based tests blocked step-1 as fake
# code bugs while the same failure in pytest E-form was env-flaked.

_DOWNLOAD_TRACEBACK = [
    "Traceback (most recent call last):",
    '  File "/usr/local/python3.12.13/lib/python3.12/site-packages/vllm/'
    'transformers_utils/repo_utils.py", line 299, in get_model_path',
    "    return snapshot_download(model_id=model, **common_kwargs)",
    '  File "/usr/local/python3.12.13/lib/python3.12/site-packages/modelscope/'
    'hub/snapshot_download.py", line 295, in _snapshot_download',
    "    raise ValueError(",
    "ValueError: Cannot find the requested files in the cached path and "
    "outgoing traffic has been disabled. To enable look-ups and downloads "
    "online, set 'local_files_only' to False.",
]


def test_download_pure_modelscope_traceback_classified(tmp_path: Path) -> None:
    log = _write_log(tmp_path, "\n".join(_DOWNLOAD_TRACEBACK) + "\n")
    assert rt._is_model_download_failure(log)


def test_download_with_e_form_real_error_not_classified(tmp_path: Path) -> None:
    # run 31952700363 shape: a real raised error next to download noise stays
    # blocking (the ANSI-stripped E-form must still be seen).
    log = _write_log(tmp_path, "\n".join(
        _DOWNLOAD_TRACEBACK
        + ["E   \x1b[31mValueError\x1b[0m: too many values to unpack "
           "(expected 20)"]) + "\n")
    assert not rt._is_model_download_failure(log)


def test_download_with_raw_terminal_real_error_not_classified(
        tmp_path: Path) -> None:
    log = _write_log(tmp_path, "\n".join(_DOWNLOAD_TRACEBACK + [
        "Traceback (most recent call last):",
        "    ...",
        "ValueError: too many values to unpack (expected 20)",
    ]) + "\n")
    assert not rt._is_model_download_failure(log)


def test_download_server_wrapper_only_classified(tmp_path: Path) -> None:
    # pause_resume shape: the server child died of the download error (raw
    # traceback teed) and the harness re-raised a generic wrapper.
    log = _write_log(tmp_path, "\n".join(_DOWNLOAD_TRACEBACK + [
        "E   RuntimeError: vllm server exited during startup (exit code 1); "
        "see server logs"]) + "\n")
    assert rt._is_model_download_failure(log)


def test_download_dp_worker_wrapper_only_classified(tmp_path: Path) -> None:
    # four_card pipeline_parallel shape.
    log = _write_log(tmp_path, "\n".join(_DOWNLOAD_TRACEBACK + [
        "E   RuntimeError: Failed to start data parallel worker 0"]) + "\n")
    assert rt._is_model_download_failure(log)


def test_server_wrapper_without_download_signature_not_classified(
        tmp_path: Path) -> None:
    # Child output not teed — the death cannot be attributed, stays blocking.
    log = _write_log(tmp_path, "E   RuntimeError: vllm server exited during "
                               "startup (exit code 1)\n")
    assert not rt._is_model_download_failure(log)


def test_download_no_download_signature_not_classified(tmp_path: Path) -> None:
    log = _write_log(tmp_path, "E   AssertionError: garbage output\n")
    assert not rt._is_model_download_failure(log)


# ---- selection-leak safety net: exit 4 + "ERROR: not found:" --------------
# Parameter-set drift keeps the function name, so the static case-level drop
# can't see it; the runtime net turns the unfixable exit-4 into an env flake.

def test_run_one_test_not_found_becomes_env_flake(monkeypatch,
                                                  tmp_path: Path) -> None:
    log = _write_log(
        tmp_path,
        "collecting ... ERROR: not found: tests/e2e/pull_request/two_card/"
        "spec_decode/test_spec_decode.py::test_eagle3_sliding_window\n",
        "nf.log")
    summary = _write_log(tmp_path, "", "nf-summary.json")
    monkeypatch.setattr(rt, "_run_to_log", lambda *a, **k: 4)
    monkeypatch.setattr(rt, "_run_summary",
                        lambda *a, **k: {"summary": {"code_bugs": [],
                                                     "env_flakes": []},
                                         "summary_error": None})
    result = _run_one_test(
        ["pytest"], log, summary,
        "tests/e2e/pull_request/two_card/spec_decode/test_spec_decode.py::"
        "test_eagle3_sliding_window",
        "0,1", tmp_path / "ci", tmp_path, 0, 1, {},
        is_remote=False, is_mock=False)
    assert result["ci_result"] == "env_flake_pass"
    assert result["run_suite_exit_code"] == 4


def test_run_one_test_exit4_usage_error_stays_failed(monkeypatch,
                                                     tmp_path: Path) -> None:
    log = _write_log(tmp_path, "ERROR: usage: pytest [options] [file_or_tests]\n",
                     "u4.log")
    summary = _write_log(tmp_path, "", "u4-summary.json")
    monkeypatch.setattr(rt, "_run_to_log", lambda *a, **k: 4)
    monkeypatch.setattr(rt, "_run_summary",
                        lambda *a, **k: {"summary": {"code_bugs": [],
                                                     "env_flakes": []},
                                         "summary_error": None})
    result = _run_one_test(
        ["pytest"], log, summary, "tests/e2e/pull_request/one_card/test_x.py",
        "0,1", tmp_path / "ci", tmp_path, 0, 1, {},
        is_remote=False, is_mock=False)
    assert result["ci_result"] == "failed"
