"""Baseline ref + PR target wiring of push_to_github / flow.push_to_github."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from main2main_flow.scripts.utils import push_to_github


# ---- _push_via_proxy: pack-size guards (negotiation seed + 413 fallback) ----

def _ok(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


def test_push_via_proxy_seeds_absent_remote_tip(monkeypatch,
                                                tmp_path: Path) -> None:
    # Fresh-mode histories lack the fork's destination tip; without it the
    # push pack degenerates to a full tree (HTTP 413 at the proxy).  A
    # shallow fetch of just that tip must happen before the push.
    (tmp_path / "r").mkdir()
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        args = list(cmd)
        if "ls-remote" in args:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="abc1234567890 refs/heads/b1\n", stderr="")
        if "cat-file" in args:
            seen["catfile_tip"] = args[-1]
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="")
        if "fetch" in args:
            seen["seed"] = args
            return _ok(cmd)
        if "push" in args and "m2m-push" in args:
            seen["push"] = args
            return _ok(cmd)
        return _ok(cmd)

    monkeypatch.setattr(push_to_github.subprocess, "run", fake_run)
    monkeypatch.setenv("GH_TOKEN", "t0k")
    push_to_github._push_via_proxy(tmp_path / "r", "org/fork",
                                   "HEAD:refs/heads/b1", "--force")
    assert seen["catfile_tip"].endswith("abc1234567890^{commit}")
    seed = seen["seed"]
    assert "--depth" in seed and "1" in seed
    assert "refs/heads/b1" in seed
    assert any("gh-proxy.test.osinfra.cn" in str(a) for a in seed)
    # The push itself still ran (after the seed).
    assert seen["push"]


def test_push_via_proxy_skips_seed_when_tip_present(monkeypatch,
                                                    tmp_path: Path) -> None:
    # Normal case: the client already has the remote tip — no seed fetch.
    (tmp_path / "r").mkdir()
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        args = list(cmd)
        calls.append(args)
        if "ls-remote" in args:
            return subprocess.CompletedProcess(
                cmd, 0, stdout="abc1234567890 refs/heads/b1\n", stderr="")
        if "cat-file" in args:
            return _ok(cmd)  # tip exists locally
        if "push" in args and "m2m-push" in args:
            return _ok(cmd)
        return _ok(cmd)

    monkeypatch.setattr(push_to_github.subprocess, "run", fake_run)
    monkeypatch.setenv("GH_TOKEN", "t0k")
    push_to_github._push_via_proxy(tmp_path / "r", "org/fork",
                                   "HEAD:refs/heads/b1", "--force")
    assert not any("fetch" in c for c in calls)


def test_push_via_proxy_413_falls_back_to_direct(monkeypatch,
                                                 tmp_path: Path) -> None:
    # HTTP 413 from the proxy is deterministic (body-size rejection) — the
    # same refspec must be retried direct against github.com, whose
    # token-in-URL form bypasses the runner's insteadOf rewrite.
    (tmp_path / "r").mkdir()
    seen: dict = {}

    def fake_run(cmd, **kwargs):
        args = list(cmd)
        if "push" in args and "m2m-push" in args:
            seen["proxy"] = args
            return subprocess.CompletedProcess(
                cmd, 128, stdout="",
                stderr="error: RPC failed; HTTP 413 curl 22 The requested "
                       "URL returned error: 413\n"
                       "fatal: the remote end hung up unexpectedly")
        direct = [a for a in args
                  if a.startswith("https://x-access-token:")
                  and a.endswith("@github.com/org/fork.git")]
        if "push" in args and direct:
            seen["direct_url"] = direct[0]
            return _ok(cmd)
        return _ok(cmd)

    monkeypatch.setattr(push_to_github.subprocess, "run", fake_run)
    monkeypatch.setenv("GH_TOKEN", "t0k")
    push_to_github._push_via_proxy(tmp_path / "r", "org/fork",
                                   "HEAD:refs/heads/b1", "--force")
    assert "proxy" in seen
    assert seen["direct_url"] == "https://x-access-token:t0k@github.com/org/fork.git"


def test_push_via_proxy_413_direct_failure_continues(monkeypatch,
                                                     tmp_path: Path) -> None:
    # Direct fallback failing (e.g. egress blocked) must not abort the
    # retry loop — later proxy attempts still happen.
    (tmp_path / "r").mkdir()
    proxy_pushes: list[int] = []

    def fake_run(cmd, **kwargs):
        args = list(cmd)
        if "push" in args and "m2m-push" in args:
            proxy_pushes.append(len(proxy_pushes))
            return subprocess.CompletedProcess(
                cmd, 128, stdout="",
                stderr="error: RPC failed; HTTP 413 curl 22 "
                       "The requested URL returned error: 413")
        direct = [a for a in args
                  if a.startswith("https://x-access-token:")
                  and a.endswith("@github.com/org/fork.git")]
        if "push" in args and direct:
            return subprocess.CompletedProcess(
                cmd, 128, stdout="", stderr="Could not resolve host")
        return _ok(cmd)

    monkeypatch.setattr(push_to_github.subprocess, "run", fake_run)
    monkeypatch.setattr(push_to_github.time, "sleep", lambda s: None)
    monkeypatch.setenv("GH_TOKEN", "t0k")
    with pytest.raises(subprocess.CalledProcessError):
        push_to_github._push_via_proxy(tmp_path / "r", "org/fork",
                                       "HEAD:refs/heads/b1", "--force")
    assert len(proxy_pushes) == 6  # all proxy attempts were made


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
