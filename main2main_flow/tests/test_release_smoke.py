"""Regression pins for the release-tag e2e smoke in the final quality gate:

- the smoke runs once per tree AFTER the main-lane e2e is green, memoized
  by tree sha (passed and upstream-inherited trees are not re-smoked)
- a failed smoke retries once on the same tree (flake absorption) before
  triage; an adapter fix round is only burned for OWN-diff failures
- upstream-inherited failures (traceback files entirely outside the
  adaptation diff — the PR 16382 shape) are recorded, not blocking
- remote e2e mode / missing release worktree / no smoke cases all
  self-skip without touching the main-lane env
- env injection: PYTHONPATH points at the release worktree and
  VLLM_VERSION at the release tag for the duration of run_tests only
"""

import inspect
import json
import os
from pathlib import Path
from types import SimpleNamespace

from main2main_flow import flow as flow_mod


def _make_flow():
    f = flow_mod.Main2MainFlow()
    f.state.steps = [{"id": "step-1", "start_commit": "aaa", "end_commit": "bbb"}]
    f.state.total_steps = 1
    f.state.current_step = 0
    f.state.last_step_is_noop = False
    return f


def _gate_flow(monkeypatch, tmp_path):
    """Gate flow with static checks green and the e2e leg off by default."""
    monkeypatch.setattr(flow_mod, "WORKSPACE_DIR", tmp_path)
    f = _make_flow()
    f.state.vllm_ascend_path = str(tmp_path)
    f.state.last_step_e2e_passed = True
    monkeypatch.setattr(flow_mod, "run_final_quality_gate",
                        lambda **kw: (True, []))
    monkeypatch.setattr(flow_mod, "run_git", lambda *a, **k: "")
    return f


def _script_smoke(monkeypatch, f, results):
    calls: list[Path] = []

    def fake(gate_dir):
        calls.append(gate_dir)
        return results[min(len(results), len(calls)) - 1].copy()

    monkeypatch.setattr(f, "_run_release_smoke", fake)
    return calls


def _capture_adapter(monkeypatch):
    payloads: list[dict] = []

    def fake_adapter(payload, session_id=None):
        payloads.append(payload)
        return SimpleNamespace(session_id=None, is_noop=False,
                               step_summary="", modified_files=[])

    monkeypatch.setattr(flow_mod, "run_opencode_adapter", fake_adapter)
    return payloads


def _smoke_result(passed, detail="release-detail.json"):
    return {"skipped": False, "reason": "", "passed": passed,
            "detail_files": [] if passed else [detail], "result": {}}


def test_gate_smoke_pass_runs_once(monkeypatch, tmp_path):
    f = _gate_flow(monkeypatch, tmp_path)
    calls = _script_smoke(monkeypatch, f, [_smoke_result(True)])
    assert f._final_quality_gate() is True
    assert len(calls) == 1


def test_gate_smoke_flake_absorbed_no_adapter(monkeypatch, tmp_path):
    # First attempt fails, same-tree retry passes -> no adapter round.
    f = _gate_flow(monkeypatch, tmp_path)
    calls = _script_smoke(monkeypatch, f,
                          [_smoke_result(False), _smoke_result(True)])
    payloads = _capture_adapter(monkeypatch)
    assert f._final_quality_gate() is True
    assert len(calls) == 2
    assert payloads == []


def test_gate_smoke_own_failure_triggers_adapter_then_converges(
        monkeypatch, tmp_path):
    # Each scripted fail costs two smoke calls (initial + same-tree flake
    # retry) before triage routes it to the adapter; the fifth call passes
    # on the fixed tree.  The smoke detail files reach the adapter as
    # error_logs.
    f = _gate_flow(monkeypatch, tmp_path)
    calls = _script_smoke(monkeypatch, f,
                          [_smoke_result(False), _smoke_result(False),
                           _smoke_result(False), _smoke_result(False),
                           _smoke_result(True)])
    payloads = _capture_adapter(monkeypatch)
    # Each adapter round re-arms the regression e2e (fixes_applied).
    monkeypatch.setattr(f, "_run_e2e_test_for_final_gate", lambda: True)
    monkeypatch.setattr(f, "_classify_smoke_failure",
                        lambda smoke: ("own", ["vllm_ascend/worker/x.py"]))
    assert f._final_quality_gate() is True
    assert len(calls) == 5
    assert len(payloads) == 2
    assert json.loads(payloads[0]["error_logs"]) == ["release-detail.json"]
    assert payloads[0]["role"] == "adapter-fix"
    assert payloads[0]["step_id"] == "final-quality-gate"


def test_gate_smoke_own_failure_exhausts_budget(monkeypatch, tmp_path):
    # Deterministic own-diff failure: every round costs two smoke calls
    # (initial + flake retry) and one adapter round.  Iteration 1 skips
    # the e2e leg (last step passed); iterations 2-5 each burn one of the
    # 4 e2e attempts re-verifying the fix, so the 6th round breaks on the
    # e2e budget before the shared 5-round budget can be exceeded again.
    f = _gate_flow(monkeypatch, tmp_path)
    calls = _script_smoke(monkeypatch, f, [_smoke_result(False)] * 12)
    payloads = _capture_adapter(monkeypatch)
    monkeypatch.setattr(f, "_run_e2e_test_for_final_gate", lambda: True)
    monkeypatch.setattr(f, "_classify_smoke_failure",
                        lambda smoke: ("own", ["vllm_ascend/worker/x.py"]))
    assert f._final_quality_gate() is False
    assert len(payloads) == 5
    assert len(calls) == 10


def test_gate_smoke_inherited_records_and_does_not_block(monkeypatch, tmp_path):
    # Both attempts fail (initial + flake retry) but the root cause is
    # outside the adaptation diff -> recorded as evidence, no adapter
    # round, gate passes.
    f = _gate_flow(monkeypatch, tmp_path)
    calls = _script_smoke(monkeypatch, f,
                          [_smoke_result(False), _smoke_result(False)])
    payloads = _capture_adapter(monkeypatch)
    monkeypatch.setattr(
        f, "_classify_smoke_failure",
        lambda smoke: ("inherited",
                       ["vllm_ascend/worker/block_table.py"]))
    assert f._final_quality_gate() is True
    assert len(calls) == 2
    assert payloads == []
    evidence = json.loads(
        (tmp_path / "quality_gate" / "release_smoke_inherited.json")
        .read_text(encoding="utf-8"))
    assert evidence["root_cause_files"] == [
        "vllm_ascend/worker/block_table.py"]
    assert evidence["detail_files"] == ["release-detail.json"]


def test_gate_smoke_never_runs_when_e2e_regression_blocks(monkeypatch, tmp_path):
    # Ordering pin: the smoke only runs after the main-lane e2e is green
    # on that tree; a deterministic e2e regression reverts and loops
    # before the smoke is ever reached.
    f = _gate_flow(monkeypatch, tmp_path)
    f.state.last_step_e2e_passed = False
    monkeypatch.setattr(f, "_run_e2e_test_for_final_gate", lambda: False)
    calls = _script_smoke(monkeypatch, f, [_smoke_result(False)])
    _capture_adapter(monkeypatch)
    # e2e burns 4 attempts as two fail+retry pairs, then the gate breaks.
    assert f._final_quality_gate() is False
    assert len(calls) == 0


def test_release_smoke_skips_without_release_worktree(monkeypatch, tmp_path):
    f = _gate_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(f, "_release_gate_path", lambda: None)
    smoke = f._run_release_smoke(tmp_path)
    assert smoke == {"skipped": True, "reason": "no release worktree"}


def test_release_smoke_skips_when_release_gate_disabled(monkeypatch, tmp_path):
    f = _gate_flow(monkeypatch, tmp_path)
    f.state.vllm_release_path = str(tmp_path / "vllm-release")
    monkeypatch.setenv("MAIN2MAIN_RELEASE_GATE", "0")
    smoke = f._run_release_smoke(tmp_path)
    assert smoke == {"skipped": True, "reason": "no release worktree"}


def test_release_smoke_skips_without_cases(monkeypatch, tmp_path):
    f = _gate_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(f, "_release_gate_path", lambda: "/tmp/vllm-release")
    monkeypatch.setattr(flow_mod, "_resolve_release_smoke_cases", lambda: [])
    smoke = f._run_release_smoke(tmp_path)
    assert smoke == {"skipped": True, "reason": "no release smoke cases"}


def test_release_smoke_skips_under_skip_e2e(monkeypatch, tmp_path):
    f = _gate_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(f, "_release_gate_path", lambda: "/tmp/vllm-release")
    monkeypatch.setattr(flow_mod, "_resolve_release_smoke_cases",
                        lambda: ["t.py::c"])
    monkeypatch.setenv("SKIP_E2E_TEST", "true")
    smoke = f._run_release_smoke(tmp_path)
    assert smoke == {"skipped": True, "reason": "SKIP_E2E_TEST"}


def test_release_smoke_skips_in_remote_mode(monkeypatch, tmp_path):
    # Remote mode clones its own trees inside the container: the local
    # release worktree does not exist there, and only VLLM_* env is
    # forwarded — a bare VLLM_VERSION would make vllm_version_is report
    # the release tag while the engine runs main vllm.  Skip, don't lie.
    f = _gate_flow(monkeypatch, tmp_path)
    monkeypatch.setattr(f, "_release_gate_path", lambda: "/tmp/vllm-release")
    monkeypatch.setattr(flow_mod, "_resolve_release_smoke_cases",
                        lambda: ["t.py::c"])
    monkeypatch.setenv("MAIN2MAIN_RUN_TESTS_REMOTE", "http://runner:5000")
    smoke = f._run_release_smoke(tmp_path)
    assert smoke == {"skipped": True, "reason": "remote e2e mode"}


def test_release_smoke_injects_and_restores_env(monkeypatch, tmp_path):
    f = _gate_flow(monkeypatch, tmp_path)
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "vllm-release-tag.commit").write_text(
        "v0.28.0\n", encoding="utf-8")
    monkeypatch.setattr(f, "_release_gate_path", lambda: "/tmp/vllm-release")
    monkeypatch.setattr(flow_mod, "_resolve_release_smoke_cases",
                        lambda: ["t.py::c"])
    monkeypatch.setenv("PYTHONPATH", "/orig")
    monkeypatch.delenv("VLLM_VERSION", raising=False)
    seen: dict = {}

    def fake_run_tests(**kw):
        seen["pythonpath"] = os.environ["PYTHONPATH"]
        seen["vllm_version"] = os.environ.get("VLLM_VERSION")
        seen["kw"] = kw
        return {"can_commit": True, "suite_results": {}}

    monkeypatch.setattr(flow_mod, "run_tests", fake_run_tests)
    smoke = f._run_release_smoke(tmp_path / "gate")
    assert smoke["passed"] is True
    assert not smoke["skipped"]
    assert seen["pythonpath"] == "/tmp/vllm-release:/orig"
    assert seen["vllm_version"] == "0.28.0"
    # The main-lane env is restored for the rest of the gate.
    assert os.environ["PYTHONPATH"] == "/orig"
    assert "VLLM_VERSION" not in os.environ
    kw = seen["kw"]
    assert kw["skip_setup"] is True
    assert kw["patch_path"] is None
    assert kw["remote"] is None
    assert kw["test_cases"] == ["t.py::c"]
    assert kw["step_id"] == "final-quality-gate-release-smoke"
    assert kw["log_dir"] == str(tmp_path / "gate")


def test_release_smoke_failure_builds_detail_files(monkeypatch, tmp_path):
    f = _gate_flow(monkeypatch, tmp_path)
    (tmp_path / ".github").mkdir()
    monkeypatch.setattr(f, "_release_gate_path", lambda: "/tmp/vllm-release")
    monkeypatch.setattr(flow_mod, "_resolve_release_smoke_cases",
                        lambda: ["t.py::c"])
    detail = tmp_path / "errors.md"

    def fake_run_tests(**kw):
        return {"can_commit": False, "suite_results": {
            "s1": {"ci_result": "failed", "log_path": str(tmp_path / "s.log")}}}

    def fake_detail(suite_results, round_number, tests_dir, result_json):
        detail.write_text("x", encoding="utf-8")
        return detail

    monkeypatch.setattr(flow_mod, "run_tests", fake_run_tests)
    monkeypatch.setattr(flow_mod, "build_test_errors_detail", fake_detail)
    smoke = f._run_release_smoke(tmp_path / "gate")
    assert smoke["passed"] is False
    assert smoke["detail_files"] == [str(detail),
                                     str(tmp_path / "gate"
                                         / "final-quality-gate-release-smoke"
                                         / "tests" / "round-0-result.json")]


def test_release_raw_tag_reads_tag_file(monkeypatch, tmp_path):
    f = _gate_flow(monkeypatch, tmp_path)
    assert f._release_raw_tag() == ""
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "vllm-release-tag.commit").write_text(
        " v0.28.0\n", encoding="utf-8")
    assert f._release_raw_tag() == "v0.28.0"


def test_resolve_release_smoke_cases_env_overrides_policy(monkeypatch):
    monkeypatch.delenv("MAIN2MAIN_RELEASE_TEST_CASES", raising=False)
    assert len(flow_mod._resolve_release_smoke_cases()) == 3
    monkeypatch.setenv("MAIN2MAIN_RELEASE_TEST_CASES",
                       "a.py::x\n  b.py::y  ")
    assert flow_mod._resolve_release_smoke_cases() == ["a.py::x", "b.py::y"]


def _log_suite(tmp_path, log_text, ci_result="failed"):
    log = tmp_path / "suite.log"
    log.write_text(log_text, encoding="utf-8")
    return {"result": {"suite_results": {
        "s1": {"ci_result": ci_result, "log_path": str(log)}}}}


def test_smoke_failure_files_normalizes_and_excludes(monkeypatch, tmp_path):
    f = _gate_flow(monkeypatch, tmp_path)
    (tmp_path / "vllm_ascend" / "worker").mkdir(parents=True)
    # Absolute traceback path inside the ascend checkout -> relative;
    # absolute path OUTSIDE it (installed vllm) -> excluded; bare
    # `path.py:line:` mention -> kept as-is.
    log_text = (
        f'  File "{tmp_path}/vllm_ascend/worker/block_table.py", line 96,'
        f" in compute_slot_mappings\n"
        f'  File "/usr/lib/python3/site-packages/vllm/engine.py", line 1\n'
        f"vllm_ascend/patch/foo.py:123: error\n")
    smoke = _log_suite(tmp_path, log_text)
    assert f._smoke_failure_files(smoke) == [
        "vllm_ascend/patch/foo.py", "vllm_ascend/worker/block_table.py"]


def test_smoke_failure_files_ignores_passed_suites(monkeypatch, tmp_path):
    f = _gate_flow(monkeypatch, tmp_path)
    smoke = _log_suite(tmp_path, "vllm_ascend/worker/x.py:1: boom",
                       ci_result="passed")
    assert f._smoke_failure_files(smoke) == []


def test_classify_smoke_failure_states(monkeypatch, tmp_path):
    f = _gate_flow(monkeypatch, tmp_path)
    log_text = (
        f'  File "{tmp_path}/vllm_ascend/worker/block_table.py", line 96\n')
    monkeypatch.setattr(flow_mod, "run_git",
                        lambda *a, **k: "vllm_ascend/worker/block_table.py\n")
    assert f._classify_smoke_failure(_log_suite(tmp_path, log_text)) == (
        "own", ["vllm_ascend/worker/block_table.py"])

    # The 16382 shape: root-cause file exists but is NOT in this
    # adaptation's diff -> upstream-inherited.
    monkeypatch.setattr(flow_mod, "run_git",
                        lambda *a, **k: "vllm_ascend/worker/other.py\n")
    assert f._classify_smoke_failure(_log_suite(tmp_path, log_text)) == (
        "inherited", ["vllm_ascend/worker/block_table.py"])

    # No files extractable at all -> conservative "own".
    assert f._classify_smoke_failure({"result": {"suite_results": {}}}) == (
        "own", [])


def test_release_smoke_policy_is_pinned_to_allowlist():
    policy_path = Path(inspect.getfile(flow_mod)).parent / "test_policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    cases = policy["release_smoke"]
    assert len(cases) == 3
    for case in cases:
        assert case in policy["allowlist"]
    # The graph-mode node is the 16382 crash site (spec-decode capture).
    assert any("test_qwen3_dense_graph_mode" in c for c in cases)
    assert any("test_mtp_spec_decoding" in c for c in cases)
