import os
import platform
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def ts_print(*args, **kwargs) -> None:
    """Print with [HH:MM:SS.mmm] timestamp prefix.

    flush=True by default — GH Actions logs are buffered when Python's stdout
    is line/newline-buffered, so a ts_print at T shows up in the log file at
    T+seconds-to-minutes, scrambling the apparent order of events.
    """
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}"
    # Force flush unless caller explicitly passes flush=False.
    kwargs.setdefault("flush", True)
    print(f"[{ts}]", *args, **kwargs)

UpgradeCompleted = "UpgradeCompleted"
UpgradeFailed = "UpgradeFailed"
# A step exhausted its pre_ci/e2e retries: the run stops short of the
# target, but every step BEFORE the failure passed pre_ci + e2e and is
# committed — that partial adaptation is shippable as a PR (the final
# gate re-verifies it against the last verified vllm commit).
UpgradePartial = "UpgradePartial"
HasCommit = "HasCommit"
HasNoCommit = "HasNoCommit"

import os as _os
_ws_env = _os.environ.get("MAIN2MAIN_WORKSPACE", "")
WORKSPACE_DIR = Path(_ws_env) if _ws_env else (Path(__file__).parent.parent.parent / "workspace")
DETECT_FILE = "detect.json"
STEPS_FILE = "steps.json"
STEPS_DIR = "steps"
VLLM_GIT_PATCH_FILE = "upstream.patch"
VLLM_GIT_CHANGED_FILES = "changed_files.txt"
PRE_CI_CHECK_FILE = "pre_ci_check.json"
EACH_STEP_SUMMARY_FILE = "step_summary.md"
EACH_STEP_TARGET_PATCH_FILE = "step_target.patch"
EACH_STEP_CODE_STRUCTURE_GUIDE_FILE = "code-structure-guide.md"
FINAL_SUMMARY_FILE = "final_summary.md"
FINAL_TARGET_PATCH_FILE = "final_target.patch"
FINAL_CODE_STRUCTURE_GUIDE_FILE = "final_code-structure-guide.md"

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


def is_git_url(path: str) -> bool:
    return path.startswith(("https://", "http://", "git@"))


def clone_repo(url: str, target: str) -> None:
    ts_print(f"[init] Cloning {url} → {target}")
    subprocess.run(["git", "clone", url, target], check=True)


def resolve_path(raw: str, name: str) -> str:
    if is_git_url(raw):
        target = WORKSPACE_DIR / "repos" / name
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True, exist_ok=True)
        clone_repo(raw, str(target))
        return str(target)
    return raw


def run_format_sh(repo: Path) -> subprocess.CompletedProcess:
    """Run ``bash format.sh ci`` and return the CompletedProcess result.

    The ``ci`` argument makes format.sh run ``--hook-stage manual``,
    matching CI's pre-commit job (pr_test.yaml) exactly — without it, the
    manual-stage hooks (markdownlint) are skipped and their issues leak
    into the PR's CI.

    Sets ``PRE_COMMIT_HOME`` to a persistent cache path so pre-commit hooks
    don't re-download environments on every invocation.

    PRE_COMMIT_HOME is FORCED to ``~/.cache/main2main-pre-commit-<arch>``:
    the CI workflow sets /tmp/main2main-pre-commit, which is wiped on every
    container start — format.sh then re-downloaded all hook environments
    each run and timed out (run 32785447082: URLError Errno 110). On the
    A2 runners ``/root/.cache`` is a bind-mounted persistent volume, so the
    environments install once and later runs complete in seconds.  The
    cache is keyed by machine architecture: the shared volume carried
    aarch64 hook binaries (ruff/typos/clang-format/actionlint) that failed
    with ``[Errno 8] Exec format error`` on the amd64 CPU runner (run
    32969478105) — each arch gets its own cache so they never cross.

    After format.sh, removes the ``gitleaks`` binary that ``gitleaks.sh``
    downloads to the repo root when the system has no gitleaks in PATH.
    This 22MB binary is a tool artifact, not an adaptation change - leaving
    it would pollute ``git add -N`` / ``git diff`` / ``git add -A`` and end
    up in the PR.
    """
    fmt_script = repo / "format.sh"
    env = os.environ.copy()
    env["PRE_COMMIT_HOME"] = str(
        Path.home() / ".cache" / f"main2main-pre-commit-{platform.machine()}")
    r = subprocess.run(
        ["bash", str(fmt_script), "ci"], cwd=str(repo),
        capture_output=True, text=True, env=env,
    )
    gitleaks_bin = repo / "gitleaks"
    if gitleaks_bin.exists():
        try:
            gitleaks_bin.unlink()
            ts_print("[format] removed downloaded gitleaks binary (tool artifact, not code)")
        except OSError:
            pass
    return r


# Generated-artifact dirs that must never enter the gate's checks or the PR
# diff — tool output, not business code.  torch.compile dumps
# ``torch_compile_debug/`` into the repo root when a2 UTs run (e.g.
# test_gdn_layerwise_kv.py); without isolation the artifacts get staged and
# the next gate round fails format on them (run 31563761175 round 3).
GENERATED_ARTIFACT_DIRS = ("torch_compile_debug/",)


def exclude_generated_artifacts(repo: Path) -> int:
    """Isolate non-business generated artifacts from the gate's checks.

    Adds ``GENERATED_ARTIFACT_DIRS`` to ``.git/info/exclude`` (local-only,
    never pollutes the repo or the PR diff — pre-commit --all-files and
    ``git add -A``/``-N`` both honor it) and unstages any artifact files
    already in the index (e.g. intent-to-add left by the regression-e2e
    patch regen).  Returns the number of unstaged files.
    """
    exclude_file = Path(repo) / ".git" / "info" / "exclude"
    try:
        content = (exclude_file.read_text(encoding="utf-8")
                   if exclude_file.exists() else "")
        missing = [p for p in GENERATED_ARTIFACT_DIRS if p not in content]
        if missing:
            exclude_file.write_text(
                content.rstrip() + "\n" + "\n".join(missing) + "\n",
                encoding="utf-8",
            )
    except OSError:
        pass
    r = subprocess.run(
        ["git", "ls-files", "--cached", "--", *GENERATED_ARTIFACT_DIRS],
        cwd=str(repo), capture_output=True, text=True,
    )
    staged = [f for f in r.stdout.splitlines() if f]
    if staged:
        subprocess.run(["git", "reset", "-q", "--", *staged],
                       cwd=str(repo), capture_output=True, text=True)
    return len(staged)
