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
    _is_oom_hang_failure,
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


def test_oom_hang_pure_oom_classified(tmp_path: Path):
    # Round-2/3 gemma4 shape: OOM worker death + a bare traceback SOURCE line
    # ("RuntimeError(" has no message) + pytest killed by the suite timeout.
    log = _write_log(tmp_path, "\n".join([
        "INFO ... adding tasks",
        OOM_WORKER_LINE,
        'raise RuntimeError(',
        "[TIMEOUT] suite exceeded 1800s, killing process group",
    ]) + "\n")
    assert _is_oom_hang_failure(log)


def test_oom_hang_ansi_colored_real_error_not_classified(tmp_path: Path):
    # ANSI codes around AssertionError must not hide a real failure
    # (prefix_caching garbage output asserts with colored pytest output).
    log = _write_log(tmp_path, "\n".join([
        OOM_WORKER_LINE,
        "E   \x1b[31mAssertionError\x1b[0m: Test0: vllm_output does not "
        "match golden",
    ]) + "\n")
    assert not _is_oom_hang_failure(log)


def test_oom_hang_non_oom_worker_error_not_classified(tmp_path: Path):
    # Round-1 gemma4 shape: engine died of the skip_gather TypeError —
    # a real adaptation bug, even though OOM text may appear elsewhere.
    log = _write_log(tmp_path, "\n".join([
        SKIP_GATHER_WORKER_LINE,
        "NPU out of memory is mentioned in a doc line",
    ]) + "\n")
    assert not _is_oom_hang_failure(log)


def test_oom_hang_mixed_worker_errors_not_classified(tmp_path: Path):
    log = _write_log(tmp_path, "\n".join([
        OOM_WORKER_LINE,
        "ERROR Worker failed with error 'AssertionError: Engine core "
        "initialization failed'",
    ]) + "\n")
    assert not _is_oom_hang_failure(log)


def test_oom_hang_no_oom_signature_not_classified(tmp_path: Path):
    log = _write_log(tmp_path, "E   AssertionError: garbage output\n")
    assert not _is_oom_hang_failure(log)


def test_run_one_test_oom_hang_becomes_env_flake(monkeypatch, tmp_path: Path):
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
