"""Regression pins for the retry-budget policy (feature/e2e-external rules):

- ai_analysis exhaustion reverts the working tree and ships as UpgradePartial
- e2e exhaustion (retry_count >= 3) reverts and ships as UpgradePartial
- budgets are hardcoded: no env-tunable pre_ci budget, no non-blocking gate
  e2e retry helper — gate regression e2e is BLOCKING and consumes a gate round
"""

import inspect

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


def test_budgets_are_hardcoded(monkeypatch):
    # cf109e5 reversals: the env-tunable pre_ci budget and the non-blocking
    # gate e2e retry helper must not exist anymore.
    assert not hasattr(flow_mod, "_pre_ci_attempt_budget")
    assert not hasattr(flow_mod, "_GATE_E2E_ATTEMPTS")
    assert not hasattr(flow_mod, "_run_regression_e2e_with_retries")
    # ai_analysis retries exactly 3 times (hardcoded, no env knob).
    assert "range(1, 4)" in inspect.getsource(flow_mod.Main2MainFlow._ai_analysis)
    # e2e exhaustion threshold stays at 3 fix rounds.
    src = inspect.getsource(flow_mod.Main2MainFlow.process_steps)
    assert "retry_count >= 3" in src


def _gate_flow(monkeypatch, tmp_path, e2e_results):
    """Gate flow with static checks always green and scripted e2e results."""
    monkeypatch.setattr(flow_mod, "WORKSPACE_DIR", tmp_path)
    f = _make_flow()
    f.state.vllm_ascend_path = str(tmp_path)
    f.state.last_step_e2e_passed = False
    monkeypatch.setattr(flow_mod, "run_final_quality_gate",
                        lambda **kw: (True, []))
    monkeypatch.setattr(flow_mod, "run_git", lambda *a, **k: "")
    calls = []

    def fake_e2e():
        calls.append(1)
        return e2e_results[min(len(e2e_results), len(calls)) - 1]

    monkeypatch.setattr(f, "_run_e2e_test_for_final_gate", fake_e2e)
    return f, calls


def test_gate_regression_e2e_fail_is_blocking(monkeypatch, tmp_path):
    # Regression e2e failure consumes a gate round (revert + retry) instead
    # of being retried aside; with all rounds burned the gate fails.
    f, calls = _gate_flow(monkeypatch, tmp_path, [False])
    reverts = []
    monkeypatch.setattr(f, "_revert_working_tree", lambda reason: reverts.append(reason))
    assert f._final_quality_gate() is False
    assert len(calls) == 4
    assert reverts == ["gate e2e regression"] * 4


def test_gate_regression_e2e_pass_no_extra_calls(monkeypatch, tmp_path):
    f, calls = _gate_flow(monkeypatch, tmp_path, [True])
    reverts = []
    monkeypatch.setattr(f, "_revert_working_tree", lambda reason: reverts.append(reason))
    assert f._final_quality_gate() is True
    assert len(calls) == 1
    assert reverts == []
