"""Regression pins for the retry-budget policy (feature/e2e-external rules):

- ai_analysis exhaustion reverts the working tree and ships as UpgradePartial
- e2e exhaustion (_MAX_E2E_ROUNDS fix rounds) reverts and ships as
  UpgradePartial
- budgets stay hardcoded: no env-tunable pre_ci budget, no non-blocking gate
  e2e retry helper
- gate regression e2e is BLOCKING: 4 total attempts, each failure retried
  ONCE on the same tree first (flake absorption — a flaky regression must
  not destroy the gate's fix work), static checks memoized by tree sha so
  a revert doesn't re-run format/mypy/UT on an already-verified tree
- the pre_ci fix loop skips a redundant format+mypy+UT round when the retry
  reproduces the already-failed tree byte-for-byte
- adapter-judged no-op steps skip their per-step e2e and advance; the
  final gate owes the run ONE regression e2e for the whole no-op streak
  (last_step_e2e_passed stays False).  A zero-source-delta FIRST attempt
  (marker bump / docs only) defers the same way; a fix round with a red
  e2e outstanding always re-runs run_tests
- an e2e fix round re-runs ONLY the cases the previous round failed, and a
  green reduced round is confirmed against the full allowlist before the
  step may commit (that is what makes the 5-round budget affordable)
- the e2e stop-loss compares the failing set at CASE granularity and needs
  _DEFAULT_E2E_STOPLOSS_ROUNDS consecutive identical rounds (run 41014231793
  lost a whole run to a one-shot verdict on a single-suite failure)
"""

import inspect
import subprocess
from types import SimpleNamespace

from main2main_flow import flow as flow_mod


def _make_flow():
    f = flow_mod.Main2MainFlow()
    f.state.steps = [{"id": "step-1", "start_commit": "aaa", "end_commit": "bbb"}]
    f.state.total_steps = 1
    f.state.current_step = 0
    return f


def test_ai_analysis_exhaustion_reverts_partial(monkeypatch):
    f = _make_flow()
    monkeypatch.setattr(f, "_ai_analysis", lambda: False)
    monkeypatch.setattr(f, "_working_tree_diff_sha", lambda path: "")
    reverts = []
    monkeypatch.setattr(f, "_revert_working_tree", lambda reason: reverts.append(reason))
    f.process_steps()
    assert reverts == ["step step-1 ai_analysis exhausted"]
    assert f.state.final_status == flow_mod.UpgradePartial
    assert f.state.current_step == 0


def test_e2e_exhaustion_reverts_partial(monkeypatch):
    f = _make_flow()
    monkeypatch.setattr(f, "_ai_analysis", lambda: True)
    # "" diff keeps the zero-progress loop guard out of the way; only the
    # e2e exhaustion path is under test here.
    monkeypatch.setattr(f, "_working_tree_diff_sha", lambda path: "")
    monkeypatch.setattr(f, "_run_e2e_test", lambda: False)
    reverts = []
    monkeypatch.setattr(f, "_revert_working_tree", lambda reason: reverts.append(reason))
    f.process_steps()
    assert reverts == ["step step-1 e2e exhausted"]
    assert f.state.final_status == flow_mod.UpgradePartial


def _e2e_with_blockings(f, blockings):
    """Fake _run_e2e_test that records the blocking suite set per round."""
    calls: list[int] = []

    def fake():
        idx = min(len(calls), len(blockings) - 1)
        f._last_e2e_blocking = sorted(blockings[idx])
        calls.append(1)
        return False

    return fake, calls


def test_e2e_stop_loss_identical_blocking_set(monkeypatch):
    # run 34018086282: a fix round that reproduces the exact same blocking
    # set cannot converge — the stop-loss reverts instead of burning the
    # whole budget.  Since 2026-10-30 a single repeat is tolerated (fix
    # rounds are the cheap, reduced ones) and the rule needs
    # _DEFAULT_E2E_STOPLOSS_ROUNDS consecutive identical rounds.
    f = _make_flow()
    monkeypatch.setattr(f, "_ai_analysis", lambda: True)
    monkeypatch.setattr(f, "_working_tree_diff_sha", lambda path: "")
    monkeypatch.delenv("MAIN2MAIN_E2E_STOPLOSS_ROUNDS", raising=False)
    fake, calls = _e2e_with_blockings(f, [{"t_whisper"}, {"t_whisper"}])
    monkeypatch.setattr(f, "_run_e2e_test", fake)
    reverts = []
    monkeypatch.setattr(f, "_revert_working_tree",
                        lambda reason: reverts.append(reason))
    f.process_steps()
    assert len(calls) == flow_mod._DEFAULT_E2E_STOPLOSS_ROUNDS
    assert reverts == ["step step-1 e2e stop-loss (unchanged failing set)"]
    assert f.state.final_status == flow_mod.UpgradePartial
    assert f.state.current_step == 0


def test_e2e_no_stop_loss_when_blocking_set_changes(monkeypatch):
    # A fix round that MOVES the outcome (different suites blocking) still
    # gets the full retry budget — deepseek_pruning passed on round 3 only
    # because its fix rounds kept changing the picture.
    f = _make_flow()
    monkeypatch.setattr(f, "_ai_analysis", lambda: True)
    monkeypatch.setattr(f, "_working_tree_diff_sha", lambda path: "")
    fake, calls = _e2e_with_blockings(
        f, [{"t_a"}, {"t_b"}, {"t_c"}, {"t_d"}, {"t_e"}])
    monkeypatch.setattr(f, "_run_e2e_test", fake)
    reverts = []
    monkeypatch.setattr(f, "_revert_working_tree",
                        lambda reason: reverts.append(reason))
    f.process_steps()
    assert len(calls) == flow_mod._MAX_E2E_ROUNDS  # full budget consumed
    assert reverts == ["step step-1 e2e exhausted"]
    assert f.state.final_status == flow_mod.UpgradePartial


def test_stop_loss_uses_case_granularity(monkeypatch):
    # Run 41014231793: the same suite (test_cpu_offloading.py) failed two
    # rounds in a row on the SAME assertion — a real non-converging picture
    # — while the suite-level rule could not tell that apart from "same
    # file, different cases" (which IS progress).  Cases moving resets the
    # identical counter; a frozen case set trips the stop-loss.
    f = _make_flow()
    monkeypatch.setattr(f, "_ai_analysis", lambda: True)
    monkeypatch.setattr(f, "_working_tree_diff_sha", lambda path: "")
    monkeypatch.delenv("MAIN2MAIN_E2E_STOPLOSS_ROUNDS", raising=False)
    rounds = [
        ({"t_cpu_offloading"}, ["...test_cpu_offloading.py::test_cpu_offloading[True]"]),
        ({"t_cpu_offloading"}, ["...test_cpu_offloading.py::test_cpu_offloading[True]",
                                "...test_cpu_offloading.py::test_cpu_offloading[False]"]),
        ({"t_cpu_offloading"}, ["...test_cpu_offloading.py::test_cpu_offloading[True]",
                                "...test_cpu_offloading.py::test_cpu_offloading[False]"]),
        ({"t_cpu_offloading"}, ["...test_cpu_offloading.py::test_cpu_offloading[True]",
                                "...test_cpu_offloading.py::test_cpu_offloading[False]"]),
    ]
    calls: list[int] = []

    def fake():
        idx = min(len(calls), len(rounds) - 1)
        suites, cases = rounds[idx]
        f._last_e2e_blocking = sorted(suites)
        f._last_e2e_cases = sorted(cases)
        calls.append(1)
        return False

    monkeypatch.setattr(f, "_run_e2e_test", fake)
    reverts = []
    monkeypatch.setattr(f, "_revert_working_tree",
                        lambda reason: reverts.append(reason))
    f.process_steps()
    # round 1 (fresh), round 2 (cases changed -> reset), rounds 3 and 4
    # identical -> the stop-loss fires on the third identical picture
    assert len(calls) == 4
    assert reverts == ["step step-1 e2e stop-loss (unchanged failing set)"]


def test_fix_round_reruns_only_failing_cases(monkeypatch, tmp_path):
    # A fix round must not pay for the whole allowlist: it re-runs the node
    # ids the previous round failed, then confirms the full set once green.
    f = _make_flow()
    f.state.changed_files = ["vllm_ascend/ops/foo.py"]
    f.state.retry_count = 1
    f.state.cur_patch_path = ""
    f._last_e2e_cases = ["tests/e2e/one.py::t"]
    f._e2e_retry_cases = ["tests/e2e/one.py::t"]
    monkeypatch.setattr(flow_mod, "WORKSPACE_DIR", tmp_path)
    (tmp_path / "steps" / "step-1").mkdir(parents=True)
    calls: list[list[str] | None] = []

    def fake_run(**kw):
        calls.append(kw["test_cases"])
        return {"can_commit": True, "ci_result": "passed", "suite_results": {}}

    monkeypatch.setattr(flow_mod, "run_tests", fake_run)
    monkeypatch.setattr(flow_mod, "build_test_errors_detail",
                        lambda *a, **k: None)
    monkeypatch.delenv("MAIN2MAIN_E2E_CONFIRM_FULL", raising=False)

    assert f._run_e2e_test() is True
    assert calls[0] == ["tests/e2e/one.py::t"]
    assert calls[1] == flow_mod._resolve_test_cases()  # the confirmation


def test_fix_round_first_attempt_runs_full_allowlist(monkeypatch, tmp_path):
    # retry_count == 0 always runs the whole allowlist, whatever stale
    # reuse list an earlier step left behind.
    f = _make_flow()
    f.state.changed_files = ["vllm_ascend/ops/foo.py"]
    f.state.retry_count = 0
    f._e2e_retry_cases = ["tests/e2e/one.py::t"]
    monkeypatch.setattr(flow_mod, "WORKSPACE_DIR", tmp_path)
    (tmp_path / "steps" / "step-1").mkdir(parents=True)
    calls: list[list[str] | None] = []
    monkeypatch.setattr(flow_mod, "run_tests", lambda **kw: (
        calls.append(kw["test_cases"]),
        {"can_commit": True, "ci_result": "passed", "suite_results": {}})[1])
    monkeypatch.setattr(flow_mod, "build_test_errors_detail",
                        lambda *a, **k: None)

    assert f._run_e2e_test() is True
    assert calls == [flow_mod._resolve_test_cases()]


def _head(repo) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True,
                          capture_output=True, text=True).stdout.strip()


def _noop_step_flow(repo, monkeypatch):
    """The old no-op shape: adapter succeeds without touching any file
    (adapt_result.is_noop would have been True under the removed skip)."""
    f = _make_flow()
    f.state.vllm_ascend_path = str(repo)
    monkeypatch.setattr(f, "_ai_analysis", lambda: True)
    return f


def test_noop_step_skips_e2e_and_advances(monkeypatch, tmp_path):
    # User model (2026-10-15): a no-op step (adapter found no vllm-ascend
    # change) skips its per-step e2e and moves straight to the next step's
    # adaptation.  The marker-bump commit still lands, but the run owes
    # the final gate one regression e2e — last_step_e2e_passed stays False.
    repo = _init_ascend_repo(tmp_path)
    base_head = _head(repo)
    # The realistic no-op tree: only the verified.commit bump, zero source
    # changes — exactly PR 18766's anatomy.
    (repo / ".github").mkdir()
    (repo / ".github" / "vllm-main-verified.commit").write_text(
        "0f8b398158f01fb3ea0e81b5adc93338fdf3db9c\n")
    f = _noop_step_flow(repo, monkeypatch)
    f.state.last_step_is_noop = True
    e2e_calls: list[int] = []
    monkeypatch.setattr(f, "_run_e2e_test",
                        lambda: e2e_calls.append(f.state.retry_count) or True)

    f.process_steps()

    assert e2e_calls == []  # the no-op step never pays a per-step e2e
    assert _head(repo) != base_head  # the marker bump still commits
    assert f.state.current_step == 1
    assert f.state.last_step_e2e_passed is False  # gate owes this run one e2e
    assert f.state.final_status == flow_mod.UpgradeCompleted


def test_noop_streak_costs_zero_per_step_e2e(monkeypatch, tmp_path):
    # N consecutive no-op steps skip every per-step e2e; the flag stays
    # False so the final quality gate runs its regression e2e exactly once
    # for the whole streak.
    repo = _init_ascend_repo(tmp_path)
    f = _make_flow()
    f.state.vllm_ascend_path = str(repo)
    f.state.steps = [
        {"id": "step-1", "start_commit": "aaa", "end_commit": "bbb"},
        {"id": "step-2", "start_commit": "bbb", "end_commit": "ccc"},
    ]
    f.state.total_steps = 2
    f.state.current_step = 0
    f.state.last_step_is_noop = True
    monkeypatch.setattr(f, "_ai_analysis", lambda: True)
    e2e_calls: list[int] = []
    monkeypatch.setattr(f, "_run_e2e_test",
                        lambda: e2e_calls.append(f.state.retry_count) or True)

    f.process_steps()

    assert e2e_calls == []  # two no-op steps, zero per-step e2e rounds
    assert f.state.current_step == 2
    assert f.state.last_step_e2e_passed is False  # → the gate's single e2e
    assert f.state.final_status == flow_mod.UpgradeCompleted


def test_non_source_first_attempt_defers_e2e_to_gate(monkeypatch, tmp_path):
    # Docs/configs-only diff on the FIRST attempt: skip the per-step e2e,
    # leave the flag False so the final gate's regression e2e covers it.
    f = _make_flow()
    f.state.changed_files = [".github/vllm-main-verified.commit", "docs/note.md"]
    f.state.retry_count = 0
    monkeypatch.setattr(flow_mod, "WORKSPACE_DIR", tmp_path)
    run_calls: list[dict] = []
    monkeypatch.setattr(flow_mod, "run_tests",
                        lambda **kw: run_calls.append(kw) or {"can_commit": True})

    assert f._run_e2e_test() is True
    assert run_calls == []
    assert f.state.last_step_e2e_passed is False


def test_non_source_fix_round_still_runs_e2e(monkeypatch, tmp_path):
    # A fix round has a RED e2e outstanding: a docs-only tweak must not
    # skip its way into a commit — run_tests is invoked regardless.
    f = _make_flow()
    f.state.changed_files = ["docs/note.md"]
    f.state.retry_count = 1
    monkeypatch.setattr(flow_mod, "WORKSPACE_DIR", tmp_path)
    run_calls: list[dict] = []
    monkeypatch.setattr(flow_mod, "run_tests",
                        lambda **kw: run_calls.append(kw) or {"can_commit": True})

    assert f._run_e2e_test() is True
    assert len(run_calls) == 1


def test_budgets_are_hardcoded(monkeypatch):
    # cf109e5 reversals: the env-tunable pre_ci budget and the non-blocking
    # gate e2e retry helper must not exist anymore.
    assert not hasattr(flow_mod, "_pre_ci_attempt_budget")
    assert not hasattr(flow_mod, "_GATE_E2E_ATTEMPTS")
    assert not hasattr(flow_mod, "_run_regression_e2e_with_retries")
    # ai_analysis retries exactly 5 times (hardcoded, no env knob) —
    # run 34078835752 converged 34→30→5 UT failures in 3 and reverted one
    # round short.
    assert "range(1, 6)" in inspect.getsource(flow_mod.Main2MainFlow._ai_analysis)
    # The gate's static (format/mypy/UT) fix budget is also 5 — same
    # "reverted one round short" family, aligned with the pre_ci budget.
    assert "fix_rounds_left = 5" in inspect.getsource(
        flow_mod.Main2MainFlow._final_quality_gate)
    # e2e exhaustion threshold keeps a hardcoded fix-round budget.
    src = inspect.getsource(flow_mod.Main2MainFlow.process_steps)
    assert "retry_count >= _MAX_E2E_ROUNDS" in src
    assert flow_mod._MAX_E2E_ROUNDS >= 5


def _gate_flow(monkeypatch, tmp_path, e2e_results):
    """Gate flow with static checks always green and scripted e2e results.

    MAIN2MAIN_EXTENDED_E2E=0 pins the LEGACY e2e fallback path (same-tree
    retry + revert semantics) — these tests document that escape hatch.
    """
    monkeypatch.setattr(flow_mod, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setenv("MAIN2MAIN_EXTENDED_E2E", "0")
    f = _make_flow()
    f.state.vllm_ascend_path = str(tmp_path)
    f.state.last_step_e2e_passed = False
    static_calls: list[int] = []

    def fake_static(**kw):
        static_calls.append(1)
        return (True, [])

    monkeypatch.setattr(flow_mod, "run_final_quality_gate", fake_static)
    monkeypatch.setattr(flow_mod, "run_git", lambda *a, **k: "")
    calls: list[int] = []

    def fake_e2e():
        calls.append(1)
        return e2e_results[min(len(e2e_results), len(calls)) - 1]

    monkeypatch.setattr(f, "_run_e2e_test_for_final_gate", fake_e2e)
    return f, calls, static_calls


def test_gate_regression_e2e_fail_is_blocking(monkeypatch, tmp_path):
    # A deterministic regression burns the 4-attempt e2e budget as two
    # same-tree pairs (fail -> same-tree retry -> revert) and then fails
    # the gate — same e2e count as the old 4-round loop.
    f, calls, static_calls = _gate_flow(monkeypatch, tmp_path, [False])
    reverts = []
    monkeypatch.setattr(f, "_revert_working_tree", lambda reason: reverts.append(reason))
    assert f._final_quality_gate() is False
    assert len(calls) == 4
    assert reverts == ["gate e2e regression"] * 2


def test_gate_regression_e2e_pass_no_extra_calls(monkeypatch, tmp_path):
    f, calls, static_calls = _gate_flow(monkeypatch, tmp_path, [True])
    reverts = []
    monkeypatch.setattr(f, "_revert_working_tree", lambda reason: reverts.append(reason))
    assert f._final_quality_gate() is True
    assert len(calls) == 1
    assert reverts == []


def test_gate_regression_flake_absorbed_no_revert(monkeypatch, tmp_path):
    # Flaky regression: the second attempt on the SAME tree passes — the
    # gate continues without a revert, so gate fix work is not destroyed.
    f, calls, static_calls = _gate_flow(monkeypatch, tmp_path, [False, True])
    reverts = []
    monkeypatch.setattr(f, "_revert_working_tree", lambda reason: reverts.append(reason))
    assert f._final_quality_gate() is True
    assert len(calls) == 2
    assert reverts == []


def test_gate_static_runs_once_when_tree_unchanged(monkeypatch, tmp_path):
    # After a revert the tree is byte-identical to the one that already
    # passed static checks — only the e2e re-runs, format/mypy/UT do not.
    f, calls, static_calls = _gate_flow(monkeypatch, tmp_path, [False])
    monkeypatch.setattr(f, "_revert_working_tree", lambda reason: None)
    assert f._final_quality_gate() is False
    assert len(calls) == 4
    assert len(static_calls) == 1


def _init_ascend_repo(tmp_path):
    repo = tmp_path / "ascend"
    repo.mkdir()
    for args in (["git", "init", "-q"],
                 ["git", "config", "user.email", "t@t"],
                 ["git", "config", "user.name", "t"]):
        subprocess.run(args, cwd=repo, check=True)
    (repo / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    return repo


def _pre_ci_flow(monkeypatch, tmp_path, repo):
    monkeypatch.setattr(flow_mod, "WORKSPACE_DIR", tmp_path / "ws")
    (tmp_path / "ws" / "steps" / "step-1").mkdir(parents=True)
    f = _make_flow()
    f.state.vllm_ascend_path = str(repo)
    f.state.vllm_path = str(tmp_path / "vllm")
    (tmp_path / "vllm").mkdir()  # _ai_analysis resets vllm via cwd=vllm_path
    f.state.release_tag = "v1"
    # retry_count=1 skips the checkout/run_update branch in _ai_analysis.
    f.state.retry_count = 1
    monkeypatch.setattr(flow_mod, "_build_upstream_fix_diff", lambda *a, **k: "")
    monkeypatch.setattr(flow_mod, "_revert_e2e_test_edits", lambda p: [])
    return f


def _fake_adapter(repo, modify_on_call=0):
    calls: list[int] = []

    def fake_adapter(payload, session_id=None):
        calls.append(1)
        if modify_on_call and len(calls) == modify_on_call:
            (repo / "fix.py").write_text("y = 2\n")
        return SimpleNamespace(session_id=None, is_noop=False,
                               step_summary="", modified_files=[])

    return fake_adapter, calls


def test_pre_ci_zero_progress_skips_redundant_pre_ci(monkeypatch, tmp_path):
    # A retry that reproduces the failed pre_ci tree byte-for-byte warns
    # on the first occurrence and fails the step on the second WITHOUT
    # burning another full format+mypy+UT round.
    repo = _init_ascend_repo(tmp_path)
    f = _pre_ci_flow(monkeypatch, tmp_path, repo)
    fake_adapter, adapter_calls = _fake_adapter(repo)
    monkeypatch.setattr(flow_mod, "run_opencode_adapter", fake_adapter)
    check_calls: list[int] = []

    def fake_check(*a, **k):
        check_calls.append(1)
        return {"all_passed": False,
                "checks": [{"name": "ut", "passed": False, "detail": "boom"}]}

    monkeypatch.setattr(flow_mod, "run_check", fake_check)

    assert f._ai_analysis() is False
    assert len(adapter_calls) == 3
    assert len(check_calls) == 1  # attempts 2/3 skipped the redundant pre_ci
    warn = (tmp_path / "ws" / "steps" / "step-1"
            / "zero-progress-pre-ci-warning.txt")
    assert warn.exists()
    assert f.state.test_errors
    assert "zero-progress-pre-ci-warning" in f.state.test_errors[0]


def test_pre_ci_zero_progress_recovers_after_real_fix(monkeypatch, tmp_path):
    # After the warning, a retry that REALLY changes the tree re-runs
    # pre_ci and can still pass — the guard only skips identical trees.
    repo = _init_ascend_repo(tmp_path)
    f = _pre_ci_flow(monkeypatch, tmp_path, repo)
    fake_adapter, adapter_calls = _fake_adapter(repo, modify_on_call=3)
    monkeypatch.setattr(flow_mod, "run_opencode_adapter", fake_adapter)
    check_calls: list[int] = []

    def fake_check(*a, **k):
        check_calls.append(1)
        if len(check_calls) == 1:
            return {"all_passed": False,
                    "checks": [{"name": "ut", "passed": False, "detail": "boom"}]}
        return {"all_passed": True, "checks": []}

    monkeypatch.setattr(flow_mod, "run_check", fake_check)
    monkeypatch.setattr(f, "_run_adapter_qa", lambda **kw: ([], [], None))

    assert f._ai_analysis() is True
    assert len(adapter_calls) == 3
    assert len(check_calls) == 2  # warning round skipped pre_ci; real fix re-ran it
    assert (tmp_path / "ws" / "steps" / "step-1"
            / "zero-progress-pre-ci-warning.txt").exists()
