"""Regression pins for the post-gate extended e2e phase:

- switch/env/step guards self-skip without touching run_tests
- the first round runs the resolved (ordered) extended set; a fix round
  re-runs ONLY the failed suites (a full re-run costs hours on ~100 files)
- failures enter adapter-fix rounds (MAIN2MAIN_EXTENDED_FIX_ROUNDS) with
  the failed-suite error detail; pytest exit 4 (collection error) suites
  are excluded from fix rounds permanently — the adapter cannot fix imports
- budgets: fix rounds, wall-clock backstop (MAIN2MAIN_EXTENDED_MAX_MIN),
  and a stop-loss on an identical failing set (gate/step loop semantics)
- statics re-run after a fix round edits the tree (sha change), with one
  adapter retry; a final static failure ends the phase but KEEPS the fixes
- the phase is best-effort: exhausting every budget never fails the run;
  fixes are committed and gate_final_patch regenerated so the PR
  description carries them
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from main2main_flow import flow as flow_mod
from main2main_flow.scripts.utils.utils import (
    EXTENDED_E2E_DIR, EXTENDED_E2E_RESULT_FILE)

CASE_A = "tests/e2e/pull_request/one_card/test_a.py"
CASE_B = "tests/e2e/pull_request/one_card/test_b.py"
CASE_C = "tests/e2e/pull_request/two_card/test_c.py"


def _extended_flow(monkeypatch, tmp_path, cases=(CASE_A, CASE_B, CASE_C)):
    """Flow with the case resolution and heavy collaborators scripted.

    Both sources resolve to the same trio: the fixed policy key (default
    mode) via _resolve_extended_policy_cases + prune_fixed_set, and the
    whole-label resolver (MODE=full) via resolve_extended_cases.
    """
    monkeypatch.setattr(flow_mod, "WORKSPACE_DIR", tmp_path)
    f = flow_mod.Main2MainFlow()
    f.state.steps = [{"id": "step-1", "start_commit": "aaa",
                      "end_commit": "bbb"}]
    f.state.total_steps = 1
    f.state.current_step = 1
    f.state.vllm_ascend_path = str(tmp_path)
    f.state.vllm_path = str(tmp_path)
    f.state.release_tag = ""
    f.state.session_id = None
    f.state.cur_vllm_commit = "bbb"
    f.state.cur_ascend_commit = "ccc"
    f.state.last_verified_commit = "bbb"
    f.state.original_ascend_ref = "base"

    monkeypatch.setattr(flow_mod, "_resolve_extended_policy_cases",
                        lambda: list(cases))
    monkeypatch.setattr(flow_mod, "prune_fixed_set",
                        lambda ascend, cs, fixed: {
                            "cases": list(cs), "dropped_fixed": [],
                            "dropped_skip": [], "dropped_310p": [],
                            "dropped_missing": []})
    monkeypatch.setattr(flow_mod, "resolve_extended_cases", lambda *a, **k: {
        "cases": list(cases), "tier1": [],
        "dropped_missing": [], "dropped_skip": [], "dropped_310p": [],
        "source": "override"})
    monkeypatch.setattr(flow_mod, "partition_by_import_closure",
                        lambda cases, mods, path: ([], list(cases)))
    monkeypatch.setattr(flow_mod, "order_cases",
                        lambda cases, tier1, times: list(cases))
    monkeypatch.setattr(flow_mod, "_load_estimated_times", lambda p: {})
    monkeypatch.setattr(flow_mod, "run_git", lambda *a, **k: "")
    monkeypatch.setattr(f, "_release_gate_path", lambda: "")
    # Not a git repo -> the real implementation returns ""; give a stable
    # sentinel so memoization comparisons are deterministic.
    monkeypatch.setattr(f, "_working_tree_diff_sha",
                        lambda path="": "sha-stable")
    return f


def _suite(test, result="failed", exit_code=1):
    return (test, {"ci_result": result, "run_suite_exit_code": exit_code,
                   "log_path": "", "summary_path": ""})


def _rt_result(suites, elapsed=1.0):
    return {"ci_result": ("passed" if all(
        s["ci_result"] in flow_mod.PASS_RESULTS for s in suites.values())
        else "failed"),
        "can_commit": False, "suite_results": suites, "elapsed_s": elapsed}


def _script_run_tests(monkeypatch, f, outcomes):
    """run_tests returns outcomes[i] on the i-th call; records case lists."""
    calls: list[dict] = []

    def fake(**kw):
        calls.append({"test_cases": list(kw.get("test_cases") or []),
                      "log_dir": kw.get("log_dir"),
                      "round_number": kw.get("round_number"),
                      "preserve_order": kw.get("preserve_order")})
        return outcomes[min(len(outcomes) - 1, len(calls) - 1)]

    monkeypatch.setattr(flow_mod, "run_tests", fake)
    return calls


def _capture_adapter(monkeypatch, f):
    payloads: list[dict] = []

    def fake(role, error_logs, gate_dir):
        payloads.append({"role": role, "error_logs": list(error_logs)})
        return SimpleNamespace(session_id=None)

    monkeypatch.setattr(f, "_gate_adapter_fix", fake)
    return payloads


def _fake_git_ok(monkeypatch):
    """subprocess.run succeeds for git commands (commit/add on tmp tree)."""
    cmds: list[list] = []

    def fake_run(cmd, **kw):
        cmds.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(flow_mod.subprocess, "run", fake_run)
    return cmds


def _read_result(tmp_path) -> dict:
    return json.loads((tmp_path / EXTENDED_E2E_DIR / EXTENDED_E2E_RESULT_FILE)
                      .read_text(encoding="utf-8"))


def test_disabled_switch_skips(monkeypatch, tmp_path):
    f = _extended_flow(monkeypatch, tmp_path)
    monkeypatch.setenv("MAIN2MAIN_EXTENDED_E2E", "0")
    calls = _script_run_tests(monkeypatch, f, [])
    assert f._run_extended_e2e() == {"status": "disabled"}
    assert calls == []


def test_skip_e2e_test_env_skips(monkeypatch, tmp_path):
    f = _extended_flow(monkeypatch, tmp_path)
    monkeypatch.setenv("SKIP_E2E_TEST", "true")
    calls = _script_run_tests(monkeypatch, f, [])
    out = f._run_extended_e2e()
    assert out["status"] == "skipped"
    assert calls == []


def test_zero_steps_skips(monkeypatch, tmp_path):
    f = _extended_flow(monkeypatch, tmp_path)
    f.state.current_step = 0
    calls = _script_run_tests(monkeypatch, f, [])
    assert f._run_extended_e2e()["status"] == "skipped"
    assert calls == []


def test_empty_case_set(monkeypatch, tmp_path):
    f = _extended_flow(monkeypatch, tmp_path, cases=[])
    calls = _script_run_tests(monkeypatch, f, [])
    out = f._run_extended_e2e()
    assert out["status"] == "empty"
    assert calls == []
    assert _read_result(tmp_path)["status"] == "empty"


def test_fixed_mode_reads_policy_key(monkeypatch, tmp_path):
    # Default mode: the curated fixed set from test_policy.json, with the
    # allowlist-drift/skip/missing guards applied (prune_fixed_set).
    f = _extended_flow(monkeypatch, tmp_path)
    calls = _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "passed"), _suite(CASE_B, "passed"),
                         _suite(CASE_C, "passed")]))])
    out = f._run_extended_e2e()
    assert out["status"] == "passed"
    assert out["mode"] == "fixed"
    assert out["source"] == "test_policy.json extended_e2e"
    assert calls[0]["test_cases"] == [CASE_A, CASE_B, CASE_C]


def test_full_mode_uses_resolver(monkeypatch, tmp_path):
    f = _extended_flow(monkeypatch, tmp_path)
    _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "passed")]))])
    monkeypatch.setenv("MAIN2MAIN_EXTENDED_MODE", "full")
    out = f._run_extended_e2e()
    assert out["status"] == "passed"
    assert out["mode"] == "full"
    assert out["source"] == "override"  # scripted resolver's source


def test_override_env_skips_policy_and_resolver(monkeypatch, tmp_path):
    # MAIN2MAIN_EXTENDED_TEST_CASES replaces both sources entirely; the
    # drift guards still apply to it (stale overrides must not waste NPU).
    f = _extended_flow(monkeypatch, tmp_path)
    _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "passed")]))])
    monkeypatch.setenv("MAIN2MAIN_EXTENDED_TEST_CASES", f"{CASE_B} {CASE_C}")
    seen: dict = {}

    def fake_prune(ascend, cs, fixed):
        seen["cases"] = list(cs)
        return {"cases": list(cs), "dropped_fixed": [],
                "dropped_skip": [], "dropped_310p": [],
                "dropped_missing": []}

    monkeypatch.setattr(flow_mod, "prune_fixed_set", fake_prune)
    out = f._run_extended_e2e()
    assert seen["cases"] == [CASE_B, CASE_C]
    assert out["mode"] == "override"


def test_drift_guard_drops_allowlist_covered(monkeypatch, tmp_path):
    # A policy entry the per-step allowlist already covers is dropped at
    # runtime (curation is offline; the two lists drift over time).
    f = _extended_flow(monkeypatch, tmp_path,
                       cases=[CASE_A, CASE_B, CASE_C])
    calls = _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "passed")]))])
    monkeypatch.setattr(flow_mod, "prune_fixed_set",
                        lambda ascend, cs, fixed: {
                            "cases": [CASE_A], "dropped_fixed": [CASE_B],
                            "dropped_skip": [CASE_C], "dropped_310p": [],
                            "dropped_missing": []})
    out = f._run_extended_e2e()
    assert out["status"] == "passed"
    assert calls[0]["test_cases"] == [CASE_A]
    assert out["dropped_fixed"] == [CASE_B]
    assert out["dropped_skip"] == [CASE_C]


def test_first_pass_green_no_adapter(monkeypatch, tmp_path):
    f = _extended_flow(monkeypatch, tmp_path)
    calls = _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "passed"), _suite(CASE_B, "passed"),
                         _suite(CASE_C, "passed")]))])
    payloads = _capture_adapter(monkeypatch, f)
    out = f._run_extended_e2e()
    assert out["status"] == "passed"
    assert len(calls) == 1
    assert calls[0]["test_cases"] == [CASE_A, CASE_B, CASE_C]
    assert payloads == []
    data = _read_result(tmp_path)
    assert data["status"] == "passed"
    assert data["cases_total"] == 3
    # Green phase: no fixes, so no commit attempt and no gate patch write.
    assert data["final"]["committed"] is False


def test_fix_round_reruns_failed_suites_only(monkeypatch, tmp_path):
    f = _extended_flow(monkeypatch, tmp_path)
    calls = _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "passed"), _suite(CASE_B, "failed"),
                         _suite(CASE_C, "failed")])),
        _rt_result(dict([_suite(CASE_B, "passed"), _suite(CASE_C, "passed")]))])
    payloads = _capture_adapter(monkeypatch, f)
    monkeypatch.setattr(flow_mod, "build_test_errors_detail",
                        lambda *a, **k: None)
    monkeypatch.setenv("MAIN2MAIN_EXTENDED_FIX_ROUNDS", "2")
    cmds = _fake_git_ok(monkeypatch)
    out = f._run_extended_e2e()
    assert out["status"] == "passed"
    assert len(calls) == 2
    # Round 2 re-runs ONLY the failed suites, in the fixed set's spirit of
    # bounded cost (the fixed-set full-re-run policy governs per-step e2e,
    # not this phase).
    assert calls[1]["test_cases"] == [CASE_B, CASE_C]
    assert len(payloads) == 1
    assert payloads[0]["role"] == "adapter-fix"
    # Fixes are committed and the cumulative patch regenerated.
    assert any("commit" in c and
               any("extended e2e fixes" in a for a in c) for c in cmds)
    assert (tmp_path / "gate_final_patch").exists()
    data = _read_result(tmp_path)
    assert data["final"]["committed"] is True
    assert data["final"]["gate_final_patch_regenerated"] is True


def test_env_restored_after_phase(monkeypatch, tmp_path):
    f = _extended_flow(monkeypatch, tmp_path)
    _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "passed")]))])
    _capture_adapter(monkeypatch, f)
    monkeypatch.setenv("SKIP_PIP_INSTALL", "sentinel-pip")
    monkeypatch.setenv("MAIN2MAIN_KEEP_BRANCH", "sentinel-branch")
    f._run_extended_e2e()
    assert os.environ["SKIP_PIP_INSTALL"] == "sentinel-pip"
    assert os.environ["MAIN2MAIN_KEEP_BRANCH"] == "sentinel-branch"


def test_identical_failing_set_stop_loss(monkeypatch, tmp_path):
    f = _extended_flow(monkeypatch, tmp_path)
    failing = dict([_suite(CASE_A, "failed"), _suite(CASE_B, "passed")])
    calls = _script_run_tests(monkeypatch, f, [_rt_result(failing)])
    payloads = _capture_adapter(monkeypatch, f)
    monkeypatch.setattr(flow_mod, "build_test_errors_detail",
                        lambda *a, **k: None)
    out = f._run_extended_e2e()
    assert out["status"] == "stop_loss_no_progress"
    assert len(calls) == 2  # initial round + one fix round, then stop-loss
    assert len(payloads) == 1


def test_collection_error_excluded_from_fix_rounds(monkeypatch, tmp_path):
    f = _extended_flow(monkeypatch, tmp_path)
    calls = _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "failed", exit_code=4),
                         _suite(CASE_B, "passed")]))])
    payloads = _capture_adapter(monkeypatch, f)
    out = f._run_extended_e2e()
    # The ONLY real failure was the collection error — nothing left to
    # fix, phase ends passed with the error recorded.
    assert out["status"] == "passed"
    assert len(calls) == 1
    assert payloads == []
    data = _read_result(tmp_path)
    assert data["collection_errors"] == [CASE_A]


def test_collection_error_plus_real_failure_fixes_real_only(
        monkeypatch, tmp_path):
    f = _extended_flow(monkeypatch, tmp_path)
    calls = _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "failed", exit_code=4),
                         _suite(CASE_B, "failed"),
                         _suite(CASE_C, "passed")])),
        _rt_result(dict([_suite(CASE_B, "passed")]))])
    payloads = _capture_adapter(monkeypatch, f)
    monkeypatch.setattr(flow_mod, "build_test_errors_detail",
                        lambda *a, **k: Path("/tmp/detail.txt"))
    monkeypatch.setenv("MAIN2MAIN_EXTENDED_FIX_ROUNDS", "2")
    _fake_git_ok(monkeypatch)
    out = f._run_extended_e2e()
    assert out["status"] == "passed"
    assert calls[1]["test_cases"] == [CASE_B]  # exit-4 suite NOT re-run
    assert len(payloads) == 1
    # error_logs carry the detail file + the round result json.
    assert payloads[0]["error_logs"][0] == "/tmp/detail.txt"


def test_fix_budget_exhausted_keeps_fixes(monkeypatch, tmp_path):
    f = _extended_flow(monkeypatch, tmp_path)
    monkeypatch.setenv("MAIN2MAIN_EXTENDED_FIX_ROUNDS", "0")
    calls = _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "failed")]))])
    payloads = _capture_adapter(monkeypatch, f)
    out = f._run_extended_e2e()
    assert out["status"] == "exhausted"
    assert len(calls) == 1
    assert payloads == []  # zero budget -> no adapter round at all
    data = _read_result(tmp_path)
    assert data["final"]["failing"] == [CASE_A]


def test_time_budget_reached(monkeypatch, tmp_path):
    f = _extended_flow(monkeypatch, tmp_path)
    monkeypatch.setenv("MAIN2MAIN_EXTENDED_MAX_MIN", "360")
    _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "failed")]))])
    _capture_adapter(monkeypatch, f)
    monkeypatch.setattr(flow_mod, "build_test_errors_detail",
                        lambda *a, **k: None)
    # The deadline is computed from monotonic() at phase start; a huge
    # "now" on the next read fires the backstop before the fix round.
    reads = {"n": 0}
    real_monotonic = flow_mod.time.monotonic

    def fake_monotonic():
        reads["n"] += 1
        return 0.0 if reads["n"] <= 1 else real_monotonic() + 10**9

    monkeypatch.setattr(flow_mod.time, "monotonic", fake_monotonic)
    out = f._run_extended_e2e()
    assert out["status"] == "time_budget"


def test_static_failure_after_fix_keeps_fixes(monkeypatch, tmp_path):
    # The tree sha changes after the adapter fix -> statics re-run and
    # stay failing -> one static adapter round -> verification still
    # failing -> phase ends static_failed with fixes committed.
    f = _extended_flow(monkeypatch, tmp_path)
    sha = {"v": "sha-0"}

    monkeypatch.setattr(f, "_working_tree_diff_sha",
                        lambda path="": sha["v"])

    def fix_and_edit(role, error_logs, gate_dir):
        fix_and_edit.calls.append(role)
        sha["v"] = f"sha-{len(fix_and_edit.calls)}"

    fix_and_edit.calls = []
    monkeypatch.setattr(f, "_gate_adapter_fix", fix_and_edit)
    _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "failed")]))])
    static_calls: list[bool] = []

    def fake_static(**kw):
        static_calls.append(False)
        return (False, ["static-error.log"])

    monkeypatch.setattr(flow_mod, "run_final_quality_gate", fake_static)
    monkeypatch.setattr(flow_mod, "build_test_errors_detail",
                        lambda *a, **k: None)
    monkeypatch.setenv("MAIN2MAIN_EXTENDED_FIX_ROUNDS", "2")
    _fake_git_ok(monkeypatch)
    out = f._run_extended_e2e()
    assert out["status"] == "static_failed"
    assert len(static_calls) == 2  # initial static check + verification
    assert len(fix_and_edit.calls) == 2  # e2e fix round + static fix round
    data = _read_result(tmp_path)
    assert data["final"]["committed"] is True  # fixes kept, never reverted


def test_static_recheck_green_continues(monkeypatch, tmp_path):
    # Fix edits the tree, statics re-run GREEN, and the phase continues to
    # a passing round.
    f = _extended_flow(monkeypatch, tmp_path)
    sha = {"v": "sha-0"}
    monkeypatch.setattr(f, "_working_tree_diff_sha", lambda path="": sha["v"])

    def fix_then_edit(role, error_logs, gate_dir):
        sha["v"] = "sha-1"

    monkeypatch.setattr(f, "_gate_adapter_fix", fix_then_edit)
    calls = _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "failed")])),
        _rt_result(dict([_suite(CASE_A, "passed")]))])
    monkeypatch.setattr(flow_mod, "run_final_quality_gate",
                        lambda **kw: (True, []))
    monkeypatch.setattr(flow_mod, "build_test_errors_detail",
                        lambda *a, **k: None)
    monkeypatch.setenv("MAIN2MAIN_EXTENDED_FIX_ROUNDS", "2")
    _fake_git_ok(monkeypatch)
    out = f._run_extended_e2e()
    assert out["status"] == "passed"
    assert len(calls) == 2
    data = _read_result(tmp_path)
    assert [s["passed"] for s in data["statics"]] == [True]


def test_exception_never_fails_run(monkeypatch, tmp_path):
    f = _extended_flow(monkeypatch, tmp_path)

    def boom(**kw):
        raise RuntimeError("npu exploded")

    monkeypatch.setattr(flow_mod, "run_tests", boom)
    out = f._run_extended_e2e()
    assert out["status"] == "error"
    assert "npu exploded" in out["error"]


def test_extended_summary_line(tmp_path, monkeypatch):
    monkeypatch.setattr(flow_mod, "WORKSPACE_DIR", tmp_path)
    phase = tmp_path / EXTENDED_E2E_DIR
    phase.mkdir(parents=True)
    (phase / EXTENDED_E2E_RESULT_FILE).write_text(json.dumps({
        "status": "exhausted", "cases_total": 95, "rounds": [{}, {}],
        "collection_errors": ["x.py"],
        "final": {"failing": ["a.py", "b.py"]}}), encoding="utf-8")
    line = flow_mod._extended_e2e_summary()
    assert "95 case(s)" in line
    assert "exhausted" in line
    assert "2 suite(s) still failing" in line
    assert "1 collection-error suite(s)" in line
    # No evidence file -> empty (never promise unexecuted coverage).
    (phase / EXTENDED_E2E_RESULT_FILE).unlink()
    assert flow_mod._extended_e2e_summary() == ""
    (phase / EXTENDED_E2E_RESULT_FILE).write_text(
        json.dumps({"status": "disabled"}), encoding="utf-8")
    assert flow_mod._extended_e2e_summary() == ""


@pytest.mark.parametrize("env,expected", [
    ("0", "disabled"), ("false", "disabled"), ("1", None)])
def test_switch_parsing(monkeypatch, tmp_path, env, expected):
    f = _extended_flow(monkeypatch, tmp_path)
    _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "passed"), _suite(CASE_B, "passed"),
                         _suite(CASE_C, "passed")]))])
    monkeypatch.setenv("MAIN2MAIN_EXTENDED_E2E", env)
    out = f._run_extended_e2e()
    if expected:
        assert out["status"] == expected
    else:
        # "1" proceeds past the switch and runs the resolved trio.
        assert out["status"] != "disabled"
        assert out["status"] == "passed"
