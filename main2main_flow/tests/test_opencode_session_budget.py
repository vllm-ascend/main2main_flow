"""Session-budget pins: a runaway session kill is FINAL (no retry loop).

run 34018086282: attempt-1 burned 80min — a 60min total-timeout kill whose
rc=-9 was then retried as a "hard failure" with a continue prompt, giving a
second full session.  User requirement 2026-09-06: adapt/fix sessions must
stay within ~20min — met by SKILL/lessons arrangement and the sanctioned
ut_verify closure, NOT by a 20min kill.  The wall cap stays at its 60min
runaway-backstop value; what this file pins is that a kill never triggers
a retry (the mechanism that actually made the system unusable).
"""
import subprocess

from main2main_flow.scripts.agent import opencode_adapter as oa


def _init_repo(tmp_path):
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


def _inputs(tmp_path, repo):
    return {
        "role": "adapter", "mode": "adapt", "step_id": "step-1",
        "is_last_step": "false", "start_commit": "a", "end_commit": "b",
        "error_logs": "", "vllm_report_context": "x", "flow_repo": "/f",
        "ascend_path": str(repo), "vllm_path": "/v", "release_tag": "v1",
        "patch_path": "/p.diff", "step_summary_path": "/s.md",
        "step_dir": str(tmp_path), "code_structure_guide_file": "g.md",
        "changed_files_path": "/c.txt", "previous_step_summary_path": "/ps.md",
    }


def test_session_budget_default_is_runaway_backstop(monkeypatch):
    # 60min backstop — the ~20min target is met by work arrangement, not a kill
    monkeypatch.delenv("MAIN2MAIN_ADAPTER_TIMEOUT_MINUTES", raising=False)
    assert oa._TIMEOUT_MINUTES == 60


def test_total_timeout_is_final_no_retry(monkeypatch, tmp_path):
    repo = _init_repo(tmp_path)
    calls = []

    def fake_run_once(prompt, log_path, raw_path, stderr_path,
                      session_id=None, model=None):
        calls.append(1)
        return (["json-event"], "total_timeout", "sess-1", -9)

    monkeypatch.setattr(oa, "_run_once", fake_run_once)
    result = oa.run_opencode_adapter(_inputs(tmp_path, repo))
    assert len(calls) == 1  # no continue-prompt retry after a budget kill
    assert result.session_id == "sess-1"
    assert result.is_noop is True  # clean repo → partial-work detection intact


def test_stale_timeout_still_continues(monkeypatch, tmp_path):
    # a stalled session (not a budget kill) keeps its continue-retries
    repo = _init_repo(tmp_path)
    calls = []

    def fake_run_once(prompt, log_path, raw_path, stderr_path,
                      session_id=None, model=None):
        calls.append(1)
        if len(calls) == 1:
            return (["json-event"], "stale_timeout", "sess-1", -9)
        return (["json-event"], None, "sess-1", 0)

    monkeypatch.setattr(oa, "_run_once", fake_run_once)
    oa.run_opencode_adapter(_inputs(tmp_path, repo))
    assert len(calls) == 2
