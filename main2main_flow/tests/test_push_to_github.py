"""Baseline ref + PR target wiring of push_to_github / flow.push_to_github."""
from __future__ import annotations

from pathlib import Path

import pytest

from main2main_flow.scripts.utils import push_to_github


def test_update_baseline_ref_default(monkeypatch, tmp_path: Path) -> None:
    pushed: list[tuple] = []
    monkeypatch.setattr(push_to_github, "_push_via_proxy",
                        lambda ascend, fork, refspec, *a: pushed.append(
                            (fork, refspec)))
    monkeypatch.delenv("MAIN2MAIN_BASELINE_REF", raising=False)
    push_to_github._update_baseline_ref(tmp_path, "fork/x", "main2main_auto_1")
    assert pushed == [("fork/x",
                       "main2main_auto_1:refs/heads/main2main_baseline")]


def test_update_baseline_ref_env_override(monkeypatch, tmp_path: Path) -> None:
    # Validation runs write a scratch ref so the production baseline
    # (main2main_baseline) stays untouched until the feature merges.
    pushed: list[tuple] = []
    monkeypatch.setattr(push_to_github, "_push_via_proxy",
                        lambda ascend, fork, refspec, *a: pushed.append(
                            (fork, refspec)))
    monkeypatch.setenv("MAIN2MAIN_BASELINE_REF", "main2main_baseline_test")
    push_to_github._update_baseline_ref(tmp_path, "fork/x", "main2main_auto_1")
    assert pushed == [("fork/x",
                       "main2main_auto_1:refs/heads/main2main_baseline_test")]


def test_update_baseline_ref_no_fork_skips(monkeypatch, tmp_path: Path) -> None:
    pushed: list[tuple] = []
    monkeypatch.setattr(push_to_github, "_push_via_proxy",
                        lambda ascend, fork, refspec, *a: pushed.append(
                            (fork, refspec)))
    push_to_github._update_baseline_ref(tmp_path, "", "main2main_auto_1")
    assert pushed == []


def test_flow_push_uses_pr_repo_override(monkeypatch) -> None:
    # GITHUB_REPO also drives the manual-review issue and the chained next
    # run; only the PR target moves to PR_REPO when set.
    captured: dict = {}

    def fake_push_and_create_pr(**kwargs):
        captured.update(kwargs)
        return "https://github.com/x/pr/1"

    from main2main_flow import flow as flow_mod
    monkeypatch.setenv("PUSH_TO_GITHUB", "true")
    monkeypatch.setenv("GITHUB_REPO", "org/issues-repo")
    monkeypatch.setenv("PR_REPO", "org/pr-repo")
    monkeypatch.setattr(flow_mod, "push_and_create_pr",
                        fake_push_and_create_pr)
    f = flow_mod.Main2MainFlow()
    assert f.push_to_github() == "https://github.com/x/pr/1"
    assert captured["github_repo"] == "org/pr-repo"


def test_flow_push_defaults_to_github_repo(monkeypatch) -> None:
    captured: dict = {}

    def fake_push_and_create_pr(**kwargs):
        captured.update(kwargs)
        return ""

    from main2main_flow import flow as flow_mod
    monkeypatch.setenv("PUSH_TO_GITHUB", "true")
    monkeypatch.setenv("GITHUB_REPO", "org/issues-repo")
    monkeypatch.delenv("PR_REPO", raising=False)
    monkeypatch.setattr(flow_mod, "push_and_create_pr",
                        fake_push_and_create_pr)
    f = flow_mod.Main2MainFlow()
    f.push_to_github()
    assert captured["github_repo"] == "org/issues-repo"


def test_flow_push_skips_without_repo(monkeypatch) -> None:
    from main2main_flow import flow as flow_mod
    monkeypatch.setenv("PUSH_TO_GITHUB", "true")
    monkeypatch.delenv("GITHUB_REPO", raising=False)
    monkeypatch.delenv("PR_REPO", raising=False)
    f = flow_mod.Main2MainFlow()
    assert f.push_to_github() == "SKIP_PUSH"
