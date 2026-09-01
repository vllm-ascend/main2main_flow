"""No-op fix-loop detection in process_steps (run 33406387872 round 3).

A fix round that reproduces the exact diff the previous e2e round already
failed must not burn another e2e round: first occurrence warns the adapter
and re-analyzes, second occurrence fails the step immediately.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from main2main_flow import flow as flow_mod


def _init_repo(tmp_path: Path) -> Path:
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


def _flow(repo: Path, tmp_path: Path, monkeypatch) -> flow_mod.Main2MainFlow:
    monkeypatch.setattr(flow_mod, "WORKSPACE_DIR", tmp_path / "ws")
    f = flow_mod.Main2MainFlow()
    f.state.vllm_ascend_path = str(repo)
    f.state.steps = [{"id": "step-1", "end_commit": "a" * 8,
                      "upstream_patch": "", "changed_files": ""}]
    f.state.total_steps = 1
    f.state.current_step = 0
    return f


def _adapt_file(repo: Path, name: str = "patched.py") -> None:
    (repo / name).write_text("y = 2\n")


def test_zero_diff_fix_loop_warns_then_fails_fast(monkeypatch, tmp_path):
    repo = _init_repo(tmp_path)
    f = _flow(repo, tmp_path, monkeypatch)

    e2e_calls: list[int] = []
    reverts: list[str] = []
    analysis_calls = {"n": 0}

    def fake_analysis():
        analysis_calls["n"] += 1
        if analysis_calls["n"] == 1:
            _adapt_file(repo)  # initial adaptation
        return True  # later attempts change nothing

    monkeypatch.setattr(f, "_ai_analysis", fake_analysis)
    monkeypatch.setattr(f, "_run_e2e_test",
                        lambda: e2e_calls.append(f.state.retry_count) or False)
    monkeypatch.setattr(f, "_revert_working_tree",
                        lambda reason: reverts.append(reason))

    f.process_steps()

    assert len(e2e_calls) == 1  # zero-diff rounds never re-run e2e
    assert len(reverts) == 1
    assert f.state.final_status == flow_mod.UpgradeFailed
    warn = tmp_path / "ws" / "steps" / "step-1" / "zero-progress-fix-warning.txt"
    assert warn.exists()
    assert f.state.test_errors[0] == str(warn)
    assert analysis_calls["n"] == 3  # warned attempt got one re-analysis


def test_zero_diff_warning_then_real_fix_reruns_e2e(monkeypatch, tmp_path):
    repo = _init_repo(tmp_path)
    f = _flow(repo, tmp_path, monkeypatch)

    e2e_calls: list[int] = []
    commits: list[list[str]] = []
    analysis_calls = {"n": 0}

    def fake_analysis():
        analysis_calls["n"] += 1
        if analysis_calls["n"] == 1:
            _adapt_file(repo)      # initial adaptation
        if analysis_calls["n"] == 3:
            _adapt_file(repo, "b.py")  # real fix after the warning
        return True

    monkeypatch.setattr(f, "_ai_analysis", fake_analysis)
    monkeypatch.setattr(f, "_run_e2e_test",
                        lambda: e2e_calls.append(f.state.retry_count)
                        or len(e2e_calls) >= 2)  # 2nd e2e passes
    # Real git must still run: the zero-diff check hashes `git diff HEAD`
    # through flow_mod.run_git, so only swallow its errors, don't stub it.
    def fake_git(repo, *args):
        r = real_run(["git", *args], cwd=str(repo),
                     capture_output=True, text=True)
        return r.stdout

    monkeypatch.setattr(flow_mod, "run_git", fake_git)
    monkeypatch.setattr(flow_mod, "submit_step_lesson", lambda *a, **k: None)
    real_run = subprocess.run

    def fake_run(cmd, **kw):
        if cmd[:2] == ["git", "commit"]:
            commits.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, "", "")
        return real_run(cmd, **kw)

    monkeypatch.setattr(flow_mod.subprocess, "run", fake_run)

    f.process_steps()

    assert e2e_calls == [0, 1]  # initial round + post-warning fix round
    assert analysis_calls["n"] == 3
    assert f.state.final_status == flow_mod.UpgradeCompleted
    assert commits  # the step was committed
    assert f.state.retry_count == 0


def test_zero_diff_sha_tracks_working_tree(monkeypatch, tmp_path):
    repo = _init_repo(tmp_path)
    f = _flow(repo, tmp_path, monkeypatch)
    s0 = f._working_tree_diff_sha(str(repo))
    (repo / "a.py").write_text("x = 2\n")
    s1 = f._working_tree_diff_sha(str(repo))
    (repo / "a.py").write_text("x = 1\n")
    s2 = f._working_tree_diff_sha(str(repo))
    assert s0 and s1 and s2
    assert s0 != s1
    assert s2 == s0


def test_zero_diff_sha_empty_on_git_failure(tmp_path):
    f = flow_mod.Main2MainFlow()
    # Not a git repository (and nothing above it is either) — helper must
    # degrade to "" so the zero-diff check never acts on unknown state.
    assert f._working_tree_diff_sha(str(tmp_path)) == ""
