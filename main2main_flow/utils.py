import fcntl
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def ts_print(*args, **kwargs) -> None:
    """Print with [HH:MM:SS.mmm] timestamp prefix."""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}"
    print(f"[{ts}]", *args, **kwargs)

UpgradeCompleted = "UpgradeCompleted"
UpgradeFailed = "UpgradeFailed"
HasCommit = "HasCommit"
HasNoCommit = "HasNoCommit"

PROJECT_ROOT = Path(__file__).parent.parent

_ws_env = os.environ.get("MAIN2MAIN_WORKSPACE", "")
WORKSPACE_DIR = Path(_ws_env) if _ws_env else (PROJECT_ROOT / "workspace")
DETECT_FILE = "detect.json"
STEPS_FILE = "steps.json"
STEPS_DIR = "steps"
VLLM_GIT_PATCH_FILE = "upstream.patch"
VLLM_GIT_CHANGED_FILES = "changed_files.txt"
PRE_CI_CHECK_FILE = "pre_ci_check.json"
EACH_STEP_SUMMARY_FILE = "step_summary.md"
EACH_STEP_TARGET_PATCH_FILE = "step_target.patch"
EACH_STEP_CODE_STRUCTURE_GUIDE_FILE = "code-structure-guide.md"
EACH_STEP_RESULT_FILE = "result.json"
EACH_STEP_REVIEW_FILE = "review.json"
FINAL_SUMMARY_FILE = "final_summary.md"
FINAL_TARGET_PATCH_FILE = "final_target.patch"
FINAL_CODE_STRUCTURE_GUIDE_FILE = "final_code-structure-guide.md"
RUNS_LEDGER_FILE = "runs-ledger.jsonl"
REFERENCE_CANDIDATES_FILE = "candidates.md"

LOCK_FILE = ".main2main.lock"


class StepFailure(Exception):
    """Step-level abort; the message is the failure reason."""


class FlowLock:
    """Non-blocking flock guarding against overlapping scheduled runs."""

    def __init__(self) -> None:
        self._path = PROJECT_ROOT / LOCK_FILE
        self._fh = None

    def __enter__(self) -> "FlowLock":
        self._fh = self._path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.seek(0)
            holder = self._fh.read().strip()
            self._fh.close()
            self._fh = None
            raise RuntimeError(
                f"another main2main run is in progress (lock {self._path}"
                + (f", pid {holder}" if holder else "") + ")"
            )
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))
        self._fh.flush()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fh is not None:
            fcntl.flock(self._fh, fcntl.LOCK_UN)
            self._fh.close()
            self._fh = None


def run_git(repo: Path | str, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        cmd = " ".join(args)
        ts_print(f"[git] FAILED: git {cmd}\n{result.stderr.strip()}", flush=True)
        result.check_returncode()
    return result.stdout


def git_intent_to_add(repo: Path | str) -> None:
    """git add -N so untracked new files show up in `git diff HEAD`."""
    result = subprocess.run(
        ["git", "add", "-N", "."],
        cwd=str(repo),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        ts_print(f"[git] add -N failed in {repo}: {result.stderr.strip()}")


def is_tree_clean(repo: Path | str) -> bool:
    return not run_git(repo, "status", "--porcelain").strip()


def tree_status_excerpt(repo: Path | str) -> str:
    lines = run_git(repo, "status", "--porcelain").strip().splitlines()
    excerpt = lines[:15]
    if len(lines) > 15:
        excerpt.append(f"... ({len(lines) - 15} more)")
    return "\n".join(excerpt)


def is_git_url(path: str) -> bool:
    return path.startswith(("https://", "http://", "git@"))


def clone_repo(url: str, target: str) -> None:
    ts_print(f"[init] Cloning {url} → {target}")
    subprocess.run(["git", "clone", url, target], check=True)


def resolve_path(raw: str, name: str) -> str:
    if is_git_url(raw):
        target = PROJECT_ROOT / "repo_cache" / name
        if target.exists():
            if not (target / ".git").exists():
                raise RuntimeError(
                    f"{target} exists but is not a git clone; remove it manually"
                )
            # Cache clones are flow-owned, never user checkouts, so a hard
            # sync to upstream is safe and keeps re-runs cheap.
            run_git(target, "fetch", "origin", "--tags", "--force", "--prune")
            run_git(target, "reset", "--hard", "origin/HEAD")
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            clone_repo(raw, str(target))
        return str(target)
    return raw
