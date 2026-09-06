"""_revert_e2e_test_edits must handle every git status shape under tests/e2e/.

`git checkout --` is a NO-OP for newly added files (staged via `git add` or
intent-to-add via `git add -N`): it exits 0 and leaves the file on disk, so
an adapter-created test file would survive the freeze guard and ship in the
adaptation PR.  Those entries need `git rm -f --cached` + unlink.
"""
import subprocess
from pathlib import Path

from main2main_flow.flow import _revert_e2e_test_edits


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True,
                   capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "checkout", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    e2e = repo / "tests" / "e2e"
    e2e.mkdir(parents=True)
    (e2e / "existing.py").write_text("GOLDEN = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")


def _status(repo: Path) -> str:
    r = subprocess.run(["git", "status", "--short", "--", "tests/e2e/"],
                       cwd=str(repo), capture_output=True, text=True)
    return r.stdout


def test_revert_covers_modified_staged_and_intent_to_add(tmp_path: Path) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)
    e2e = repo / "tests" / "e2e"
    (e2e / "existing.py").write_text("GOLDEN = 999\n", encoding="utf-8")  # tracked edit
    (e2e / "staged.py").write_text("new\n", encoding="utf-8")             # git add
    (e2e / "intent.py").write_text("new\n", encoding="utf-8")             # git add -N
    _git(repo, "add", "tests/e2e/staged.py")
    _git(repo, "add", "-N", "tests/e2e/intent.py")

    reverted = _revert_e2e_test_edits(str(repo))

    assert sorted(reverted) == [
        "tests/e2e/existing.py", "tests/e2e/intent.py", "tests/e2e/staged.py"]
    assert (e2e / "existing.py").read_text(encoding="utf-8") == "GOLDEN = 1\n"
    assert not (e2e / "staged.py").exists()
    assert not (e2e / "intent.py").exists()
    assert _status(repo).strip() == ""


def test_revert_untracked_file_and_dir(tmp_path: Path) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)
    e2e = repo / "tests" / "e2e"
    (e2e / "loose.py").write_text("x\n", encoding="utf-8")
    (e2e / "new_dir").mkdir()
    (e2e / "new_dir" / "inner.py").write_text("y\n", encoding="utf-8")

    reverted = _revert_e2e_test_edits(str(repo))

    assert sorted(reverted) == ["tests/e2e/loose.py", "tests/e2e/new_dir/"]
    assert not (e2e / "loose.py").exists()
    assert not (e2e / "new_dir").exists()


def test_revert_noop_without_e2e_dir(tmp_path: Path) -> None:
    assert _revert_e2e_test_edits(str(tmp_path / "missing")) == []
