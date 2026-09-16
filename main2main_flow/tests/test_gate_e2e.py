"""Regression pins for the gate's main e2e (_run_gate_e2e):

the former post-gate "extended e2e" phase IS the gate's regression e2e
now (2026-09-15 merge) — the wide coverage set with gate semantics:

- guards self-skip without touching run_tests; an empty resolved set is
  non-blocking ("empty")
- the first round runs the resolved (ordered) set; every later round
  re-runs ONLY the pending suites (delta)
- a first failure re-runs the failed suites once on the same tree (flake
  absorption) BEFORE triage or any adapter round
- triage (release-smoke philosophy): traceback files entirely outside the
  cumulative adaptation diff are upstream-inherited — recorded in
  gate_e2e_inherited.json, NOT blocking; own-diff failures enter
  adapter-fix rounds (MAIN2MAIN_EXTENDED_FIX_ROUNDS) and BLOCK the gate
  when the budgets exhaust (unlike the old best-effort phase)
- pytest exit 4 (collection error) suites are excluded from fix rounds
  permanently; when only they remain, the triage verdict decides blocking
- budgets: fix rounds, wall-clock backstop (MAIN2MAIN_EXTENDED_MAX_MIN),
  and a stop-loss on an identical failing set
- statics re-run after a fix round edits the tree (sha change), with one
  adapter retry; a final static failure blocks but KEEPS the fixes
- fixes stay UNCOMMITTED (uniform with every other gate fix); the gate's
  success path regenerates gate_final_patch from the working tree
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from main2main_flow import flow as flow_mod
from main2main_flow.scripts.utils.utils import (
    GATE_E2E_RESULT_FILE, GATE_E2E_INHERITED_FILE)

CASE_A = "tests/e2e/pull_request/one_card/test_a.py"
CASE_B = "tests/e2e/pull_request/one_card/test_b.py"
CASE_C = "tests/e2e/pull_request/two_card/test_c.py"
OWN_FILE = "vllm_ascend/worker/x.py"


def _gate_e2e_flow(monkeypatch, tmp_path, cases=(CASE_A, CASE_B, CASE_C),
                   diff_files: str = ""):
    """Flow with the case resolution and heavy collaborators scripted.

    Both sources resolve to the same trio: the fixed policy key (default
    mode) via _resolve_gate_e2e_policy_cases + prune_fixed_set, and the
    whole-label resolver (MODE=full) via resolve_extended_cases.
    *diff_files* is what `git diff --name-only <baseline>` returns — the
    triage's cumulative adaptation diff (empty -> every failure file is
    inherited).
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

    monkeypatch.setattr(flow_mod, "_resolve_gate_e2e_policy_cases",
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

    def fake_git(*args, **kw):
        return diff_files

    monkeypatch.setattr(flow_mod, "run_git", fake_git)
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


def _triage(monkeypatch, f, files):
    """Script the traceback-file extraction the triage consumes."""
    monkeypatch.setattr(f, "_smoke_failure_files",
                        lambda smoke: list(files))


def _read_result(tmp_path) -> dict:
    return json.loads(
        (tmp_path / "quality_gate" / GATE_E2E_RESULT_FILE)
        .read_text(encoding="utf-8"))


def test_no_steps_skips(monkeypatch, tmp_path):
    f = _gate_e2e_flow(monkeypatch, tmp_path)
    f.state.steps = []
    calls = _script_run_tests(monkeypatch, f, [])
    assert f._run_gate_e2e(tmp_path / "quality_gate")["status"] == "skipped"
    assert calls == []


def test_skip_e2e_test_env_skips(monkeypatch, tmp_path):
    f = _gate_e2e_flow(monkeypatch, tmp_path)
    monkeypatch.setenv("SKIP_E2E_TEST", "true")
    calls = _script_run_tests(monkeypatch, f, [])
    out = f._run_gate_e2e(tmp_path / "quality_gate")
    assert out["status"] == "skipped"
    assert calls == []


def test_empty_case_set(monkeypatch, tmp_path):
    f = _gate_e2e_flow(monkeypatch, tmp_path, cases=[])
    calls = _script_run_tests(monkeypatch, f, [])
    out = f._run_gate_e2e(tmp_path / "quality_gate")
    assert out["status"] == "empty"
    assert calls == []
    assert _read_result(tmp_path)["status"] == "empty"


def test_fixed_mode_reads_policy_key(monkeypatch, tmp_path):
    # Default mode: the curated fixed set from test_policy.json, with the
    # allowlist-drift/skip/missing guards applied (prune_fixed_set).
    f = _gate_e2e_flow(monkeypatch, tmp_path)
    calls = _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "passed"), _suite(CASE_B, "passed"),
                         _suite(CASE_C, "passed")]))])
    out = f._run_gate_e2e(tmp_path / "quality_gate")
    assert out["status"] == "passed"
    assert out["mode"] == "fixed"
    assert out["source"] == "test_policy.json extended_e2e"
    assert calls[0]["test_cases"] == [CASE_A, CASE_B, CASE_C]
    # No preserve_order (2026-09-16): the rolling scheduler dispatches LPT —
    # the tier ordering would queue the four-card giants behind every
    # one-card suite and stretch the makespan.
    assert calls[0]["preserve_order"] is None


def test_full_mode_uses_resolver(monkeypatch, tmp_path):
    f = _gate_e2e_flow(monkeypatch, tmp_path)
    _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "passed")]))])
    monkeypatch.setenv("MAIN2MAIN_EXTENDED_MODE", "full")
    out = f._run_gate_e2e(tmp_path / "quality_gate")
    assert out["status"] == "passed"
    assert out["mode"] == "full"
    assert out["source"] == "override"  # scripted resolver's source


def test_override_env_skips_policy_and_resolver(monkeypatch, tmp_path):
    # MAIN2MAIN_EXTENDED_TEST_CASES replaces both sources entirely; the
    # drift guards still apply to it (stale overrides must not waste NPU).
    f = _gate_e2e_flow(monkeypatch, tmp_path)
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
    out = f._run_gate_e2e(tmp_path / "quality_gate")
    assert seen["cases"] == [CASE_B, CASE_C]
    assert out["mode"] == "override"


def test_drift_guard_drops_allowlist_covered(monkeypatch, tmp_path):
    # A policy entry the per-step allowlist already covers is dropped at
    # runtime (curation is offline; the two lists drift over time).
    f = _gate_e2e_flow(monkeypatch, tmp_path,
                       cases=[CASE_A, CASE_B, CASE_C])
    calls = _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "passed")]))])
    monkeypatch.setattr(flow_mod, "prune_fixed_set",
                        lambda ascend, cs, fixed: {
                            "cases": [CASE_A], "dropped_fixed": [CASE_B],
                            "dropped_skip": [CASE_C], "dropped_310p": [],
                            "dropped_missing": []})
    out = f._run_gate_e2e(tmp_path / "quality_gate")
    assert out["status"] == "passed"
    assert calls[0]["test_cases"] == [CASE_A]
    assert out["dropped_fixed"] == [CASE_B]
    assert out["dropped_skip"] == [CASE_C]


def test_first_pass_green_no_adapter(monkeypatch, tmp_path):
    f = _gate_e2e_flow(monkeypatch, tmp_path)
    calls = _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "passed"), _suite(CASE_B, "passed"),
                         _suite(CASE_C, "passed")]))])
    payloads = _capture_adapter(monkeypatch, f)
    out = f._run_gate_e2e(tmp_path / "quality_gate")
    assert out["status"] == "passed"
    assert len(calls) == 1
    assert calls[0]["test_cases"] == [CASE_A, CASE_B, CASE_C]
    assert payloads == []
    data = _read_result(tmp_path)
    assert data["status"] == "passed"
    assert data["cases_total"] == 3
    assert data["fixes_applied"] is False


def test_flake_absorbed_by_delta_rerun(monkeypatch, tmp_path):
    # First failure -> delta re-run of just the failed suites on the same
    # tree passes: flake absorbed, no triage, no adapter round.
    f = _gate_e2e_flow(monkeypatch, tmp_path)
    calls = _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "passed"), _suite(CASE_B, "failed"),
                         _suite(CASE_C, "failed")])),
        _rt_result(dict([_suite(CASE_B, "passed"),
                         _suite(CASE_C, "passed")]))])
    payloads = _capture_adapter(monkeypatch, f)
    _triage(monkeypatch, f, [OWN_FILE])  # must never be reached
    out = f._run_gate_e2e(tmp_path / "quality_gate")
    assert out["status"] == "passed"
    assert len(calls) == 2
    assert calls[1]["test_cases"] == [CASE_B, CASE_C]
    assert payloads == []


def test_own_diff_failure_fixes_then_converges(monkeypatch, tmp_path):
    # Flake re-run still fails, the traceback files intersect the
    # adaptation diff -> OWN -> adapter-fix round -> delta re-run passes.
    # Fixes stay UNCOMMITTED (uniform with every other gate fix).
    f = _gate_e2e_flow(monkeypatch, tmp_path, diff_files=f"{OWN_FILE}\n")
    calls = _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "passed"), _suite(CASE_B, "failed"),
                         _suite(CASE_C, "failed")])),
        _rt_result(dict([_suite(CASE_B, "failed"),
                         _suite(CASE_C, "failed")])),
        _rt_result(dict([_suite(CASE_B, "passed"),
                         _suite(CASE_C, "passed")]))])
    payloads = _capture_adapter(monkeypatch, f)
    monkeypatch.setattr(flow_mod, "build_test_errors_detail",
                        lambda *a, **k: None)
    monkeypatch.setenv("MAIN2MAIN_EXTENDED_FIX_ROUNDS", "2")
    out = f._run_gate_e2e(tmp_path / "quality_gate")
    assert out["status"] == "passed"
    assert len(calls) == 3
    assert calls[1]["test_cases"] == [CASE_B, CASE_C]  # delta flake re-run
    assert calls[2]["test_cases"] == [CASE_B, CASE_C]  # delta post-fix re-run
    assert len(payloads) == 1
    assert payloads[0]["role"] == "adapter-fix"
    data = _read_result(tmp_path)
    assert data["fixes_applied"] is True
    assert data["final"]["fixes_applied"] is True
    assert data["final"]["failing"] == []


def test_inherited_failure_records_and_does_not_block(monkeypatch, tmp_path):
    # Both rounds fail but every traceback file sits OUTSIDE the
    # adaptation diff (empty diff) -> upstream-inherited: recorded as
    # evidence, no adapter round, non-blocking verdict.
    f = _gate_e2e_flow(monkeypatch, tmp_path)
    calls = _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_B, "failed")])),
        _rt_result(dict([_suite(CASE_B, "failed")]))])
    payloads = _capture_adapter(monkeypatch, f)
    _triage(monkeypatch, f, ["vllm_ascend/worker/block_table.py"])
    out = f._run_gate_e2e(tmp_path / "quality_gate")
    assert out["status"] == "inherited"
    assert out["inherited_files"] == ["vllm_ascend/worker/block_table.py"]
    assert len(calls) == 2
    assert payloads == []
    evidence = json.loads(
        (tmp_path / "quality_gate" / GATE_E2E_INHERITED_FILE)
        .read_text(encoding="utf-8"))
    assert evidence["root_cause_files"] == [
        "vllm_ascend/worker/block_table.py"]


def test_own_diff_exhausted_blocks(monkeypatch, tmp_path):
    # Zero fix budget: flake re-run still failing, own diff -> exhausted,
    # which the gate loop treats as BLOCKING.
    f = _gate_e2e_flow(monkeypatch, tmp_path, diff_files=f"{OWN_FILE}\n")
    calls = _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "failed")]))])
    payloads = _capture_adapter(monkeypatch, f)
    _triage(monkeypatch, f, [OWN_FILE])
    monkeypatch.setenv("MAIN2MAIN_EXTENDED_FIX_ROUNDS", "0")
    out = f._run_gate_e2e(tmp_path / "quality_gate")
    assert out["status"] == "exhausted"
    assert len(calls) == 2  # initial round + delta flake re-run
    assert payloads == []  # zero budget -> no adapter round at all
    assert _read_result(tmp_path)["final"]["failing"] == [CASE_A]


def test_identical_failing_set_stop_loss(monkeypatch, tmp_path):
    # Own-diff failure that survives a fix round unchanged: one adapter
    # round is spent, then the stop-loss fires before a second.
    f = _gate_e2e_flow(monkeypatch, tmp_path, diff_files=f"{OWN_FILE}\n")
    failing = dict([_suite(CASE_A, "failed"), _suite(CASE_B, "passed")])
    calls = _script_run_tests(monkeypatch, f, [_rt_result(failing)])
    payloads = _capture_adapter(monkeypatch, f)
    _triage(monkeypatch, f, [OWN_FILE])
    monkeypatch.setattr(flow_mod, "build_test_errors_detail",
                        lambda *a, **k: None)
    monkeypatch.setenv("MAIN2MAIN_EXTENDED_FIX_ROUNDS", "2")
    out = f._run_gate_e2e(tmp_path / "quality_gate")
    assert out["status"] == "stop_loss_no_progress"
    assert len(calls) == 3  # initial + flake re-run + one fix round
    assert len(payloads) == 1


def test_collection_error_only_own_blocks(monkeypatch, tmp_path):
    # The ONLY failure is a collection error (pytest exit 4) and its
    # traceback touches the diff — the adapter cannot fix imports, so no
    # fix round exists: blocking.
    f = _gate_e2e_flow(monkeypatch, tmp_path, diff_files=f"{OWN_FILE}\n")
    calls = _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "failed", exit_code=4),
                         _suite(CASE_B, "passed")]))])
    payloads = _capture_adapter(monkeypatch, f)
    _triage(monkeypatch, f, [OWN_FILE])
    out = f._run_gate_e2e(tmp_path / "quality_gate")
    assert out["status"] == "collection_error_own"
    assert len(calls) == 1
    assert payloads == []
    assert _read_result(tmp_path)["collection_errors"] == [CASE_A]


def test_collection_error_only_inherited_records(monkeypatch, tmp_path):
    # Same shape, but the collection error is upstream-inherited:
    # recorded, non-blocking.
    f = _gate_e2e_flow(monkeypatch, tmp_path)
    _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "failed", exit_code=4),
                         _suite(CASE_B, "passed")]))])
    payloads = _capture_adapter(monkeypatch, f)
    _triage(monkeypatch, f, ["tests/e2e/upstream_broken.py"])
    out = f._run_gate_e2e(tmp_path / "quality_gate")
    assert out["status"] == "inherited"
    assert payloads == []
    assert (tmp_path / "quality_gate" / GATE_E2E_INHERITED_FILE).exists()


def test_collection_error_plus_real_failure_fixes_real_only(
        monkeypatch, tmp_path):
    # Exit-4 suite is never re-run; the flake re-run covers the real
    # failure only.  The exit-4 suite still participates in triage.
    f = _gate_e2e_flow(monkeypatch, tmp_path, diff_files=f"{OWN_FILE}\n")
    calls = _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "failed", exit_code=4),
                         _suite(CASE_B, "failed"),
                         _suite(CASE_C, "passed")])),
        _rt_result(dict([_suite(CASE_B, "passed")]))])
    payloads = _capture_adapter(monkeypatch, f)
    _triage(monkeypatch, f, [OWN_FILE])
    monkeypatch.setattr(flow_mod, "build_test_errors_detail",
                        lambda *a, **k: Path("/tmp/detail.txt"))
    monkeypatch.setenv("MAIN2MAIN_EXTENDED_FIX_ROUNDS", "2")
    out = f._run_gate_e2e(tmp_path / "quality_gate")
    # The real failure passed its delta re-run, but the exit-4 suite on
    # the own diff remains -> blocking with no fix round spent on it.
    assert out["status"] == "collection_error_own"
    assert calls[1]["test_cases"] == [CASE_B]  # exit-4 suite NOT re-run
    assert payloads == []
    data = _read_result(tmp_path)
    assert data["collection_errors"] == [CASE_A]


def test_static_failure_after_fix_blocks_but_keeps_fixes(
        monkeypatch, tmp_path):
    # The tree sha changes after the adapter fix -> statics re-run and
    # stay failing -> one static adapter round -> verification still
    # failing -> static_failed (blocking) with fixes kept, never reverted.
    f = _gate_e2e_flow(monkeypatch, tmp_path, diff_files=f"{OWN_FILE}\n")
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
    _triage(monkeypatch, f, [OWN_FILE])
    static_calls: list[bool] = []

    def fake_static(**kw):
        static_calls.append(False)
        return (False, ["static-error.log"])

    monkeypatch.setattr(flow_mod, "run_final_quality_gate", fake_static)
    monkeypatch.setattr(flow_mod, "build_test_errors_detail",
                        lambda *a, **k: None)
    monkeypatch.setenv("MAIN2MAIN_EXTENDED_FIX_ROUNDS", "2")
    out = f._run_gate_e2e(tmp_path / "quality_gate")
    assert out["status"] == "static_failed"
    assert len(static_calls) == 2  # initial static check + verification
    assert len(fix_and_edit.calls) == 2  # e2e fix round + static fix round
    data = _read_result(tmp_path)
    assert data["fixes_applied"] is True  # fixes kept, never reverted
    assert data["statics_verified_sha"] is None


def test_static_recheck_green_continues(monkeypatch, tmp_path):
    # Fix edits the tree, statics re-run GREEN, and the rounds continue
    # to a passing delta re-run; the verified sha is reported so the gate
    # loop's statics memo can skip a redundant re-run.
    f = _gate_e2e_flow(monkeypatch, tmp_path, diff_files=f"{OWN_FILE}\n")
    sha = {"v": "sha-0"}
    monkeypatch.setattr(f, "_working_tree_diff_sha", lambda path="": sha["v"])

    def fix_then_edit(role, error_logs, gate_dir):
        sha["v"] = "sha-1"

    monkeypatch.setattr(f, "_gate_adapter_fix", fix_then_edit)
    calls = _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "failed")])),
        _rt_result(dict([_suite(CASE_A, "failed")])),
        _rt_result(dict([_suite(CASE_A, "passed")]))])
    _triage(monkeypatch, f, [OWN_FILE])
    monkeypatch.setattr(flow_mod, "run_final_quality_gate",
                        lambda **kw: (True, []))
    monkeypatch.setattr(flow_mod, "build_test_errors_detail",
                        lambda *a, **k: None)
    monkeypatch.setenv("MAIN2MAIN_EXTENDED_FIX_ROUNDS", "2")
    out = f._run_gate_e2e(tmp_path / "quality_gate")
    assert out["status"] == "passed"
    assert len(calls) == 3  # initial + flake re-run + post-fix delta
    data = _read_result(tmp_path)
    assert [s["passed"] for s in data["statics"]] == [True]
    assert data["statics_verified_sha"] == "sha-1"


def test_env_restored_after_run(monkeypatch, tmp_path):
    f = _gate_e2e_flow(monkeypatch, tmp_path)
    _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "passed")]))])
    _capture_adapter(monkeypatch, f)
    monkeypatch.setenv("SKIP_PIP_INSTALL", "sentinel-pip")
    monkeypatch.setenv("MAIN2MAIN_KEEP_BRANCH", "sentinel-branch")
    f._run_gate_e2e(tmp_path / "quality_gate")
    assert os.environ["SKIP_PIP_INSTALL"] == "sentinel-pip"
    assert os.environ["MAIN2MAIN_KEEP_BRANCH"] == "sentinel-branch"


def test_time_budget_reached(monkeypatch, tmp_path):
    f = _gate_e2e_flow(monkeypatch, tmp_path, diff_files=f"{OWN_FILE}\n")
    monkeypatch.setenv("MAIN2MAIN_EXTENDED_MAX_MIN", "360")
    _script_run_tests(monkeypatch, f, [
        _rt_result(dict([_suite(CASE_A, "failed")]))])
    _capture_adapter(monkeypatch, f)
    _triage(monkeypatch, f, [OWN_FILE])
    monkeypatch.setattr(flow_mod, "build_test_errors_detail",
                        lambda *a, **k: None)
    # The deadline is computed from monotonic() at entry; a huge "now" on
    # the next read fires the backstop before the fix round.
    reads = {"n": 0}
    real_monotonic = flow_mod.time.monotonic

    def fake_monotonic():
        reads["n"] += 1
        return 0.0 if reads["n"] <= 1 else real_monotonic() + 10**9

    monkeypatch.setattr(flow_mod.time, "monotonic", fake_monotonic)
    out = f._run_gate_e2e(tmp_path / "quality_gate")
    assert out["status"] == "time_budget"


def test_exception_surfaces_as_error(monkeypatch, tmp_path):
    f = _gate_e2e_flow(monkeypatch, tmp_path)

    def boom(**kw):
        raise RuntimeError("npu exploded")

    monkeypatch.setattr(flow_mod, "run_tests", boom)
    out = f._run_gate_e2e(tmp_path / "quality_gate")
    assert out["status"] == "error"
    assert "npu exploded" in out["error"]


def test_gate_e2e_summary_line(tmp_path, monkeypatch):
    monkeypatch.setattr(flow_mod, "WORKSPACE_DIR", tmp_path)
    gate_dir = tmp_path / "quality_gate"
    gate_dir.mkdir(parents=True)
    (gate_dir / GATE_E2E_RESULT_FILE).write_text(json.dumps({
        "status": "exhausted", "cases_total": 55, "rounds": [{}, {}],
        "collection_errors": ["x.py"],
        "final": {"failing": ["a.py", "b.py"]}}), encoding="utf-8")
    line = flow_mod._gate_e2e_summary()
    assert "55 case(s)" in line
    assert "exhausted" in line
    assert "2 own-diff suite(s) failing" in line
    assert "1 collection-error suite(s)" in line
    # Inherited verdict names the residual risk explicitly.
    (gate_dir / GATE_E2E_RESULT_FILE).write_text(json.dumps({
        "status": "inherited", "cases_total": 55, "rounds": [{}],
        "inherited_files": ["vllm_ascend/a.py", "vllm_ascend/b.py"],
        "final": {}}), encoding="utf-8")
    line = flow_mod._gate_e2e_summary()
    assert "status **inherited**" in line
    assert "upstream-inherited" in line
    assert "vllm_ascend/a.py" in line
    # No evidence file -> empty (never promise unexecuted coverage).
    (gate_dir / GATE_E2E_RESULT_FILE).unlink()
    assert flow_mod._gate_e2e_summary() == ""
    (gate_dir / GATE_E2E_RESULT_FILE).write_text(
        json.dumps({"status": "skipped"}), encoding="utf-8")
    assert flow_mod._gate_e2e_summary() == ""


# ---- gate-loop routing: verdicts become blocking / non-blocking ----

def _loop_flow(monkeypatch, tmp_path):
    """Gate loop with statics green, e2e unscripted, smoke scripted pass."""
    monkeypatch.setattr(flow_mod, "WORKSPACE_DIR", tmp_path)
    f = flow_mod.Main2MainFlow()
    f.state.steps = [{"id": "step-1", "start_commit": "aaa",
                      "end_commit": "bbb"}]
    f.state.total_steps = 1
    f.state.current_step = 1
    f.state.vllm_ascend_path = str(tmp_path)
    f.state.last_step_e2e_passed = False
    monkeypatch.setattr(flow_mod, "run_final_quality_gate",
                        lambda **kw: (True, []))
    monkeypatch.setattr(flow_mod, "run_git", lambda *a, **k: "")
    monkeypatch.setattr(flow_mod, "submit_gate_lesson", lambda *a, **k: None)
    smoke_calls: list[int] = []
    monkeypatch.setattr(f, "_run_release_smoke", lambda gate_dir:
                        smoke_calls.append(1) or
                        {"skipped": False, "reason": "", "passed": True,
                         "detail_files": [], "result": {}})
    return f, smoke_calls


def test_gate_loop_inherited_verdict_is_not_blocking(monkeypatch, tmp_path):
    f, smoke_calls = _loop_flow(monkeypatch, tmp_path)
    e2e_calls: list = []
    monkeypatch.setattr(f, "_run_gate_e2e",
                        lambda gate_dir: e2e_calls.append(gate_dir) or
                        {"status": "inherited"})
    assert f._final_quality_gate() is True
    assert len(e2e_calls) == 1
    assert len(smoke_calls) == 1  # the smoke still runs after e2e


def test_gate_loop_own_verdict_blocks_before_smoke(monkeypatch, tmp_path):
    f, smoke_calls = _loop_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(f, "_run_gate_e2e",
                        lambda gate_dir: {"status": "exhausted"})
    assert f._final_quality_gate() is False
    assert smoke_calls == []


def test_gate_loop_memoizes_passed_tree(monkeypatch, tmp_path):
    # After the gate e2e passes, a later re-armed regression e2e (a smoke
    # fix round re-arms it) is skipped while the tree is unchanged.
    f, smoke_calls = _loop_flow(monkeypatch, tmp_path)
    e2e_calls: list[int] = []
    monkeypatch.setattr(f, "_run_gate_e2e",
                        lambda gate_dir: e2e_calls.append(1) or
                        {"status": "passed"})
    # Smoke fails twice (initial + flake retry, triaged own), then passes
    # on the same tree after one adapter round.
    smoke = [{"skipped": False, "reason": "", "passed": False,
              "detail_files": ["d.json"], "result": {}}] * 2 + \
        [{"skipped": False, "reason": "", "passed": True,
          "detail_files": [], "result": {}}]

    def fake_smoke(gate_dir):
        smoke_calls.append(1)
        return smoke[min(len(smoke), len(smoke_calls)) - 1].copy()

    monkeypatch.setattr(f, "_run_release_smoke", fake_smoke)
    monkeypatch.setattr(f, "_classify_smoke_failure",
                        lambda smoke: ("own", [OWN_FILE]))
    payloads: list[int] = []
    monkeypatch.setattr(f, "_gate_adapter_fix",
                        lambda role, error_logs, gate_dir:
                        payloads.append(1) or SimpleNamespace(session_id=None))
    assert f._final_quality_gate() is True
    # One e2e entry despite the smoke fix re-arming the regression e2e:
    # the tree never changed (statics memo + e2e memo hits).
    assert len(e2e_calls) == 1
    assert len(smoke_calls) == 3  # fail + flake retry + pass on same tree
    assert len(payloads) == 1


def test_gate_loop_legacy_fallback_when_disabled(monkeypatch, tmp_path):
    # MAIN2MAIN_EXTENDED_E2E=0 escapes to the legacy 23-case regression
    # e2e (same-tree retry + revert semantics); the wide gate e2e is
    # never entered.
    f, smoke_calls = _loop_flow(monkeypatch, tmp_path)
    monkeypatch.setenv("MAIN2MAIN_EXTENDED_E2E", "0")
    legacy_calls: list[int] = []

    def fake_legacy():
        legacy_calls.append(1)
        return True

    monkeypatch.setattr(f, "_run_e2e_test_for_final_gate", fake_legacy)
    wide_calls: list[int] = []
    monkeypatch.setattr(f, "_run_gate_e2e",
                        lambda gate_dir: wide_calls.append(1) or
                        {"status": "passed"})
    assert f._final_quality_gate() is True
    assert len(legacy_calls) == 1
    assert wide_calls == []
