"""Regression pins for the retry-budget policy (run 33406597604):

- ai_analysis exhaustion keeps the working tree (gate continues the work)
- pre_ci attempt budget is 5 (E2E budget in process_steps stays 3)
- gate regression e2e retries 3x and never blocks the PR once static
  checks are green
"""

import pytest

from main2main_flow import flow as flow_mod


def _make_flow():
    f = flow_mod.Main2MainFlow()
    f.state.steps = [{"id": "step-1", "start_commit": "aaa", "end_commit": "bbb"}]
    f.state.total_steps = 1
    f.state.current_step = 0
    return f


def test_ai_analysis_exhaustion_keeps_working_tree(monkeypatch):
    f = _make_flow()
    monkeypatch.setattr(f, "_ai_analysis", lambda: False)
    reverts = []
    monkeypatch.setattr(f, "_revert_working_tree", lambda reason: reverts.append(reason))
    f.process_steps()
    assert reverts == []
    assert f.state.final_status == flow_mod.UpgradeFailed


def test_e2e_exhaustion_still_reverts(monkeypatch):
    f = _make_flow()
    monkeypatch.setattr(f, "_ai_analysis", lambda: True)
    f.state.last_step_is_noop = False
    monkeypatch.setattr(f, "_run_e2e_test", lambda: False)
    reverts = []
    monkeypatch.setattr(f, "_revert_working_tree", lambda reason: reverts.append(reason))
    f.process_steps()
    assert reverts == ["step step-1 e2e exhausted"]
    assert f.state.final_status == flow_mod.UpgradeFailed


def test_pre_ci_attempt_budget(monkeypatch):
    monkeypatch.delenv("MAIN2MAIN_PRE_CI_ATTEMPTS", raising=False)
    assert flow_mod._pre_ci_attempt_budget() == 5
    monkeypatch.setenv("MAIN2MAIN_PRE_CI_ATTEMPTS", "7")
    assert flow_mod._pre_ci_attempt_budget() == 7
    monkeypatch.setenv("MAIN2MAIN_PRE_CI_ATTEMPTS", "not-a-number")
    assert flow_mod._pre_ci_attempt_budget() == 5
    monkeypatch.setenv("MAIN2MAIN_PRE_CI_ATTEMPTS", "0")
    assert flow_mod._pre_ci_attempt_budget() == 1


def _gate_flow(monkeypatch, tmp_path, e2e_results):
    """Gate flow with static checks always green and scripted e2e results."""
    f = _make_flow()
    f.state.vllm_ascend_path = str(tmp_path)
    f.state.last_step_e2e_passed = False
    monkeypatch.setattr(flow_mod, "WORKSPACE_DIR", tmp_path)
    monkeypatch.setattr(flow_mod, "run_final_quality_gate",
                        lambda **kw: (True, []))
    monkeypatch.setattr(flow_mod, "run_git", lambda *a, **k: "")
    calls = []

    def fake_e2e():
        calls.append(1)
        return e2e_results[min(len(e2e_results), len(calls)) - 1]

    monkeypatch.setattr(f, "_run_e2e_test_for_final_gate", fake_e2e)
    return f, calls


def test_gate_regression_e2e_all_fail_still_passes(monkeypatch, tmp_path):
    f, calls = _gate_flow(monkeypatch, tmp_path, [False])
    assert f._final_quality_gate() is True
    assert len(calls) == flow_mod._GATE_E2E_ATTEMPTS


def test_gate_regression_e2e_pass_no_extra_calls(monkeypatch, tmp_path):
    f, calls = _gate_flow(monkeypatch, tmp_path, [True])
    assert f._final_quality_gate() is True
    assert len(calls) == 1
