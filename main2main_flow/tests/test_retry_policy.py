"""Regression pins for the retry-budget policy (feature/e2e-external rules):

- ai_analysis exhaustion reverts the working tree and ships as UpgradePartial
- e2e exhaustion (retry_count >= 3) reverts and ships as UpgradePartial
- budgets are hardcoded: no env-tunable pre_ci budget, no non-blocking gate
  e2e retry helper
- gate regression e2e is BLOCKING: 4 total attempts, each failure retried
  ONCE on the same tree first (flake absorption — a flaky regression must
  not destroy the gate's fix work), static checks memoized by tree sha so
  a revert doesn't re-run format/mypy/UT on an already-verified tree
- the pre_ci fix loop skips a redundant format+mypy+UT round when the retry
  reproduces the already-failed tree byte-for-byte
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
    f.state.last_step_is_noop = False
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
    # suite set cannot converge — stop-loss reverts immediately instead of
    # burning another 1-2h e2e round (whisper OOM: 3 e2e rounds, ~7h step).
    f = _make_flow()
    monkeypatch.setattr(f, "_ai_analysis", lambda: True)
    monkeypatch.setattr(f, "_working_tree_diff_sha", lambda path: "")
    fake, calls = _e2e_with_blockings(f, [{"t_whisper"}, {"t_whisper"}])
    monkeypatch.setattr(f, "_run_e2e_test", fake)
    reverts = []
    monkeypatch.setattr(f, "_revert_working_tree",
                        lambda reason: reverts.append(reason))
    f.process_steps()
    assert len(calls) == 2  # the third e2e round never launched
    assert reverts == ["step step-1 e2e stop-loss (unchanged blocking set)"]
    assert f.state.final_status == flow_mod.UpgradePartial
    assert f.state.current_step == 0


def test_e2e_no_stop_loss_when_blocking_set_changes(monkeypatch):
    # A fix round that MOVES the outcome (different suites blocking) still
    # gets the full retry budget — deepseek_pruning passed on round 3 only
    # because its fix rounds kept changing the picture.
    f = _make_flow()
    monkeypatch.setattr(f, "_ai_analysis", lambda: True)
    monkeypatch.setattr(f, "_working_tree_diff_sha", lambda path: "")
    fake, calls = _e2e_with_blockings(f, [{"t_a"}, {"t_b"}, {"t_c"}])
    monkeypatch.setattr(f, "_run_e2e_test", fake)
    reverts = []
    monkeypatch.setattr(f, "_revert_working_tree",
                        lambda reason: reverts.append(reason))
    f.process_steps()
    assert len(calls) == 3  # full budget consumed
    assert reverts == ["step step-1 e2e exhausted"]
    assert f.state.final_status == flow_mod.UpgradePartial


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
    # e2e exhaustion threshold stays at 3 fix rounds.
    src = inspect.getsource(flow_mod.Main2MainFlow.process_steps)
    assert "retry_count >= 3" in src


def _gate_flow(monkeypatch, tmp_path, e2e_results):
    """Gate flow with static checks always green and scripted e2e results."""
    monkeypatch.setattr(flow_mod, "WORKSPACE_DIR", tmp_path)
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
    monkeypatch.setattr(f, "_run_adapter_qa", lambda **kw: ([], ""))

    assert f._ai_analysis() is True
    assert len(adapter_calls) == 3
    assert len(check_calls) == 2  # warning round skipped pre_ci; real fix re-ran it
    assert (tmp_path / "ws" / "steps" / "step-1"
            / "zero-progress-pre-ci-warning.txt").exists()
