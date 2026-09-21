"""The CI-config write guard: .github/workflows edits never survive.

Run 35513059821: static adapter-fix rounds "fixed" failing UT by editing
.github/workflows/_e2e_nightly_single_node_560t.yaml; every downstream check
was content-blind and GitHub rejected the push 6/6 times (the PAT lacks the
workflow scope).  Three mechanical layers are pinned here:

1. adapter sessions are bracketed (snapshot/restore) — a CI-config cheat is
   reverted before the next static run can verify it green;
2. the squash chokepoints (push_to_github._force_squash; flow's
   generate_final_post uses the same guard.strip) cannot commit one;
3. push preflight aborts loudly if a committed one still exists.

Whitelist policy (user 2026-09-21): under .github/ only
vllm-main-verified.commit and vllm-release-tag.commit may change — the
guard covers composite actions, CODEOWNERS, dependabot, templates too,
not just workflows/ (GitHub's PAT scope check sees only workflows/).
"""
import subprocess
from pathlib import Path

import pytest

from main2main_flow.scripts.agent import opencode_adapter
from main2main_flow.scripts.utils import ci_config_guard as guard
from main2main_flow.scripts.utils.push_to_github import _force_squash, _git_push

WF = ".github/workflows"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                   capture_output=True, text=True)


def _init_repo(repo: Path) -> str:
    """Base commit: one workflow file pair + code + the pointer file."""
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "checkout", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / WF).mkdir(parents=True)
    (repo / WF / "a.yaml").write_text("name: nightly\n", encoding="utf-8")
    (repo / WF / "c.yaml").write_text("name: other\n", encoding="utf-8")
    (repo / ".github" / "actions" / "runner").mkdir(parents=True)
    (repo / ".github" / "actions" / "runner" / "action.yml").write_text(
        "runs:\n", encoding="utf-8")
    (repo / ".github" / "CODEOWNERS").write_text("* @upstream\n",
                                                 encoding="utf-8")
    (repo / ".github" / "vllm-main-verified.commit").write_text(
        "aaa\n", encoding="utf-8")
    (repo / "vllm_ascend").mkdir()
    (repo / "vllm_ascend" / "x.py").write_text("A = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo),
                          capture_output=True, text=True).stdout.strip()


def _wf_status(repo: Path) -> str:
    r = subprocess.run(["git", "status", "--short", "--", WF],
                       cwd=str(repo), capture_output=True, text=True)
    return r.stdout


# ---- layer 1: snapshot / restore ----

def test_restore_reverts_modified_deleted_and_new(tmp_path: Path) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)
    # A pre-snapshot edit (flow-owned state) — restore must return HERE,
    # not to HEAD.
    (repo / WF / "a.yaml").write_text("name: nightly-v2\n", encoding="utf-8")
    snap = guard.snapshot(repo)
    # The adapter session: modify tracked, delete tracked, create new,
    # stage the new one, edit a composite action + CODEOWNERS, and edit
    # real code.
    (repo / WF / "a.yaml").write_text("name: nightly-cheat\n", encoding="utf-8")
    (repo / WF / "c.yaml").unlink()
    (repo / WF / "b.yaml").write_text("name: hack\n", encoding="utf-8")
    _git(repo, "add", f"{WF}/b.yaml")
    (repo / ".github" / "actions" / "runner" / "action.yml").write_text(
        "runs: cheat\n", encoding="utf-8")
    (repo / ".github" / "CODEOWNERS").write_text("* @attacker\n",
                                                 encoding="utf-8")
    (repo / "vllm_ascend" / "x.py").write_text("A = 2\n", encoding="utf-8")

    touched = guard.restore(repo, snap, "test session")

    assert touched == [".github/CODEOWNERS",
                       ".github/actions/runner/action.yml",
                       f"{WF}/a.yaml", f"{WF}/b.yaml", f"{WF}/c.yaml"]
    assert (repo / WF / "a.yaml").read_text() == "name: nightly-v2\n"
    assert (repo / WF / "c.yaml").read_text() == "name: other\n"
    assert not (repo / WF / "b.yaml").exists()
    assert (repo / ".github" / "actions" / "runner" / "action.yml").read_text() == "runs:\n"
    assert (repo / ".github" / "CODEOWNERS").read_text() == "* @upstream\n"
    # Index cleaned (git add unstaged); only the deliberate pre-snapshot
    # a.yaml edit remains visible to git.
    status = _wf_status(repo)
    assert "b.yaml" not in status and "c.yaml" not in status
    # Real code edits are NOT the guard's business.
    assert (repo / "vllm_ascend" / "x.py").read_text() == "A = 2\n"


def test_snapshot_restore_noop_on_clean_session(tmp_path: Path) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)
    snap = guard.snapshot(repo)
    assert guard.restore(repo, snap, "test session") == []
    assert _wf_status(repo) == ""


def test_restore_removes_new_nested_workflow_file(tmp_path: Path) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)
    snap = guard.snapshot(repo)
    nested = repo / WF / "configs" / "cheat.yaml"
    nested.parent.mkdir(parents=True)
    nested.write_text("name: cheat\n", encoding="utf-8")
    touched = guard.restore(repo, snap, "test session")
    assert touched == [f"{WF}/configs/cheat.yaml"]
    assert not nested.exists()


def test_restore_leaves_allowed_pointer_files_alone(tmp_path: Path) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)
    snap = guard.snapshot(repo)
    # Flow-owned pointer bumps are the ONLY permitted .github writes.
    (repo / ".github" / "vllm-main-verified.commit").write_text(
        "bbb\n", encoding="utf-8")
    (repo / ".github" / "vllm-release-tag.commit").write_text(
        "v0.13.0\n", encoding="utf-8")

    assert guard.restore(repo, snap, "test session") == []

    assert (repo / ".github" / "vllm-main-verified.commit").read_text() == "bbb\n"
    assert (repo / ".github" / "vllm-release-tag.commit").read_text() == "v0.13.0\n"


# ---- layer 1 wiring: every adapter session is bracketed ----

def test_adapter_session_ci_edits_are_reverted(tmp_path: Path,
                                               monkeypatch) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)

    def fake_run_once(prompt, log_path, raw_path, stderr_path,
                      session_id, model=None):
        (repo / WF / "cheat.yaml").write_text("name: cheat\n",
                                              encoding="utf-8")
        (repo / WF / "a.yaml").write_text("name: nightly-cheat\n",
                                          encoding="utf-8")
        (repo / "vllm_ascend" / "x.py").write_text("A = 2\n",
                                                   encoding="utf-8")
        return (['{"type": "text", "part": {"text": "done"}}'], None, "", 0)

    monkeypatch.setattr(opencode_adapter, "_run_once", fake_run_once)
    # The real _build_prompt formats the SKILL.md template (needs the full
    # step payload) — irrelevant here: the test pins the guard bracketing.
    monkeypatch.setattr(opencode_adapter, "_build_prompt",
                        lambda inputs: ("prompt", []))
    result = opencode_adapter.run_opencode_adapter(
        {"ascend_path": str(repo), "step_dir": "", "role": "adapter-fix"})

    assert not (repo / WF / "cheat.yaml").exists()
    assert (repo / WF / "a.yaml").read_text() == "name: nightly\n"
    assert (repo / "vllm_ascend" / "x.py").read_text() == "A = 2\n"
    assert result is not None


# ---- layer 2: the squash chokepoint cannot commit a workflow edit ----

def test_force_squash_strips_ci_edits_but_keeps_code(tmp_path: Path) -> None:
    repo = tmp_path / "ascend"
    base = _init_repo(repo)
    # Commit 1: a legit code fix.  Commit 2: the workflow cheat + pointer bump.
    (repo / "vllm_ascend" / "x.py").write_text("A = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fix")
    (repo / WF / "a.yaml").write_text("name: nightly-cheat\n", encoding="utf-8")
    (repo / WF / "b.yaml").write_text("name: hack\n", encoding="utf-8")
    (repo / ".github" / "CODEOWNERS").write_text("* @attacker\n",
                                                 encoding="utf-8")
    (repo / ".github" / "vllm-main-verified.commit").write_text(
        "bbb\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "cheat")

    _force_squash(repo, base, "branch-under-test")

    r = subprocess.run(["git", "diff", "--name-only", f"{base}..HEAD"],
                       cwd=str(repo), capture_output=True, text=True)
    files = set(r.stdout.split())
    assert not any(f.startswith(WF) for f in files), files
    assert "vllm_ascend/x.py" in files
    # The pointer file is whitelisted and rides along.
    assert ".github/vllm-main-verified.commit" in files
    # Non-workflow .github files are guarded too (whitelist policy).
    assert ".github/CODEOWNERS" not in files
    assert (repo / ".github" / "CODEOWNERS").read_text() == "* @upstream\n"
    assert (repo / WF / "a.yaml").read_text() == "name: nightly\n"


# ---- layer 3: push preflight aborts on any committed forbidden .github edit ----

def test_git_push_preflight_aborts_on_committed_ci_edit(
        tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "ascend"
    base = _init_repo(repo)
    (repo / WF / "a.yaml").write_text("name: nightly-cheat\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "cheat")
    pushed: list[tuple] = []
    monkeypatch.setattr("main2main_flow.scripts.utils.push_to_github"
                        "._push_with_lease",
                        lambda ascend_path, branch: pushed.append(branch))

    with pytest.raises(SystemExit):
        _git_push(repo, "main2main_auto_x", base_ref=base)
    assert pushed == []


def test_git_push_preflight_aborts_on_committed_action_edit(
        tmp_path: Path, monkeypatch) -> None:
    # A composite action is executable code GitHub's PAT scope check never
    # sees — the preflight is the only thing standing between it and the PR.
    repo = tmp_path / "ascend"
    base = _init_repo(repo)
    (repo / ".github" / "actions" / "runner" / "action.yml").write_text(
        "runs: cheat\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "cheat")
    pushed: list[tuple] = []
    monkeypatch.setattr("main2main_flow.scripts.utils.push_to_github"
                        "._push_with_lease",
                        lambda ascend_path, branch: pushed.append(branch))

    with pytest.raises(SystemExit):
        _git_push(repo, "main2main_auto_x", base_ref=base)
    assert pushed == []


def test_git_push_preflight_allows_clean_tree(tmp_path: Path,
                                              monkeypatch) -> None:
    repo = tmp_path / "ascend"
    base = _init_repo(repo)
    (repo / "vllm_ascend" / "x.py").write_text("A = 2\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fix")
    pushed: list[str] = []
    monkeypatch.setattr("main2main_flow.scripts.utils.push_to_github"
                        "._push_with_lease",
                        lambda ascend_path, branch: pushed.append(branch))

    _git_push(repo, "main2main_auto_x", base_ref=base)
    assert pushed == ["main2main_auto_x"]
