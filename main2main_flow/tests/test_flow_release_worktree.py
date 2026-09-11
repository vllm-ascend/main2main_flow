"""Release worktree lifecycle (dual-lane validation enablement).

_prepare_release_worktree must be non-fatal: a missing tag file or a
failed worktree add degrades to main-only validation, never bricks the
run.  The fetch refspec must use the RAW tag (v-prefixed) exactly as
stored in .github/vllm-release-tag.commit.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from main2main_flow import flow as flow_mod
from main2main_flow.flow import Main2MainFlow


def _flow(tmp_path: Path, with_tag: bool = True) -> Main2MainFlow:
    ascend = tmp_path / "ascend"
    (ascend / ".github").mkdir(parents=True)
    if with_tag:
        (ascend / ".github" / "vllm-release-tag.commit").write_text(
            "v0.28.0\n", encoding="utf-8")
    vllm = tmp_path / "vllm"
    vllm.mkdir()
    f = Main2MainFlow(vllm_path=str(vllm), vllm_ascend_path=str(ascend))
    return f


def _fake_run(recorder: list, fail_second: bool = False):
    def run(cmd, **kwargs):
        recorder.append(cmd)
        if fail_second and len(recorder) >= 2:
            raise subprocess.CalledProcessError(128, cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    return run


def test_prepare_release_worktree_fetches_raw_tag_and_sets_state(
        monkeypatch, tmp_path: Path) -> None:
    f = _flow(tmp_path)
    ws = tmp_path / "workspace"
    monkeypatch.setattr(flow_mod, "WORKSPACE_DIR", ws)
    recorded: list = []
    monkeypatch.setattr(flow_mod.subprocess, "run", _fake_run(recorded))
    f._prepare_release_worktree()
    assert f.state.vllm_release_path == str(ws / "repos" / "vllm-release")
    fetch_cmds = [c for c in recorded if c[:2] == ["git", "fetch"]]
    add_cmds = [c for c in recorded if c[:3] == ["git", "worktree", "add"]]
    assert fetch_cmds, "expected a tag fetch"
    assert "refs/tags/v0.28.0:refs/tags/v0.28.0" in fetch_cmds[0]
    assert "--depth" in fetch_cmds[0]
    assert add_cmds and "v0.28.0" in add_cmds[0]
    assert add_cmds[0][-2] == str(ws / "repos" / "vllm-release")


def test_prepare_release_worktree_without_tag_file_is_nonfatal(
        monkeypatch, tmp_path: Path) -> None:
    f = _flow(tmp_path, with_tag=False)
    monkeypatch.setattr(flow_mod, "WORKSPACE_DIR", tmp_path / "workspace")
    recorded: list = []
    monkeypatch.setattr(flow_mod.subprocess, "run", _fake_run(recorded))
    f._prepare_release_worktree()
    assert f.state.vllm_release_path == ""
    assert recorded == []


def test_prepare_release_worktree_failure_is_nonfatal(
        monkeypatch, tmp_path: Path) -> None:
    f = _flow(tmp_path)
    monkeypatch.setattr(flow_mod, "WORKSPACE_DIR", tmp_path / "workspace")
    recorded: list = []
    monkeypatch.setattr(flow_mod.subprocess, "run",
                        _fake_run(recorded, fail_second=True))
    f._prepare_release_worktree()
    assert f.state.vllm_release_path == ""


def test_cleanup_release_worktree(monkeypatch, tmp_path: Path) -> None:
    f = _flow(tmp_path)
    f.state.vllm_release_path = str(tmp_path / "workspace" / "repos" / "vllm-release")
    recorded: list = []
    monkeypatch.setattr(flow_mod.subprocess, "run", _fake_run(recorded))
    f._cleanup_release_worktree()
    assert f.state.vllm_release_path == ""
    assert ["git", "worktree", "remove", "-f",
            str(tmp_path / "workspace" / "repos" / "vllm-release")] in recorded


def test_cleanup_noop_without_worktree(tmp_path: Path) -> None:
    f = _flow(tmp_path)
    # Empty state → no subprocess call, no exception.
    f._cleanup_release_worktree()
    assert f.state.vllm_release_path == ""


def test_release_gate_path_kill_switch(monkeypatch, tmp_path: Path) -> None:
    f = _flow(tmp_path)
    f.state.vllm_release_path = str(tmp_path / "wt")
    assert f._release_gate_path() == str(tmp_path / "wt")
    monkeypatch.setenv("MAIN2MAIN_RELEASE_GATE", "0")
    assert f._release_gate_path() is None
    monkeypatch.setenv("MAIN2MAIN_RELEASE_GATE", "1")
    f.state.vllm_release_path = ""
    assert f._release_gate_path() is None
