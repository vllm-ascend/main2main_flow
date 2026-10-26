import os
import platform
import re
import shutil
import signal
import subprocess
import threading
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
# Standalone reports live outside source checkouts; kickoff owns workspace copies.
INTERFACE_RUN_DIR = "interface_scan"
INTERFACE_RUNTIME_CACHE_DIR = "main2main_flow/interface-reports"
INTERFACE_QA_REPORT_FILE = "qa-review.md"
INTERFACE_METADATA_FILE = "run-metadata.json"
INTERFACE_STATUS_FILE = "run-status.json"
INTERFACE_SUMMARY_FILE = "summary.md"
INTERFACE_CACHE_MANIFEST_FILE = "cache-manifest.json"
INTERFACE_SCAN_LOG_FILE = "scan.log"
INTERFACE_REPORT_DIR = "report"
INTERFACE_REPORT_FILES = (
    "main2main-range-report.json",
    "main2main-range-report.md",
    "main2main-introduced-breaks.csv",
    "main2main-all-findings.csv",
)
DETECT_FILE = "detect.json"
STEPS_FILE = "steps.json"
STEPS_DIR = "steps"
VLLM_GIT_PATCH_FILE = "upstream.patch"
VLLM_GIT_CHANGED_FILES = "changed_files.txt"
PRE_CI_CHECK_FILE = "pre_ci_check.json"
EACH_STEP_SUMMARY_FILE = "step_summary.md"
ADAPTER_QA_FILE = "adapter-qa.md"
EACH_STEP_TARGET_PATCH_FILE = "step_target.patch"
EACH_STEP_CODE_STRUCTURE_GUIDE_FILE = "code-structure-guide.md"
FINAL_SUMMARY_FILE = "final_summary.md"
FINAL_TARGET_PATCH_FILE = "final_target.patch"
FINAL_CODE_STRUCTURE_GUIDE_FILE = "final_code-structure-guide.md"
# PR CI closed-loop watcher evidence (pr_ci_monitor.py), workspace-scoped.
PR_CI_WATCH_DIR = "pr_ci_watch"
PR_CI_WATCH_RESULT_FILE = "pr_ci_watch_result.json"
# Gate main e2e evidence (flow._run_gate_e2e), written into quality_gate/.
GATE_E2E_RESULT_FILE = "gate_e2e_result.json"
GATE_E2E_INHERITED_FILE = "gate_e2e_inherited.json"
# Run-wide verdict, written by flow.generate_final_post and read by both the
# workflow's final-status step and run_summary's job-page section.
FINAL_STATUS_FILE = "final_status.json"
# End-of-run job-page summary (run_summary.emit_run_summary): one markdown
# file per run, also appended to $GITHUB_STEP_SUMMARY.
RUN_RESULT_FILE = "run_result.md"
# Per-round pre_ci trace (run_summary.record_pre_ci_attempt), one file per
# step e2e round, holding every adapter attempt that ran under that round.
PRE_CI_ROUND_FILE_FMT = "pre_ci-round-{round}.json"

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


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# GNU timeout's exit status.  pre_ci treats it as terminal: re-running a
# hung format.sh only doubles the wait.
FORMAT_TIMEOUT_RC = 124
# format.sh finishes in seconds once the hook cache is warm; a cold hook
# environment install takes minutes.  30 min is a runaway backstop, not a
# performance budget.  Override with MAIN2MAIN_FORMAT_TIMEOUT (0 = no cap).
_DEFAULT_FORMAT_TIMEOUT_S = 1800


def _echo_pipe(pipe, chunks: list[str], prefix: str = "") -> None:
    """Drain one child pipe into *chunks*, echoing each line as it arrives."""
    try:
        for line in iter(pipe.readline, ""):
            chunks.append(line)
            shown = _ANSI_RE.sub("", line.rstrip("\n"))
            if shown:
                try:
                    ts_print(f"{prefix}{shown}")
                except OSError:
                    # The job log went away (cancellation); keep draining so
                    # the child never blocks on a full pipe.
                    pass
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _kill_process_group(proc: subprocess.Popen) -> None:
    """SIGKILL the child *and* every hook it spawned.

    The child runs in its own session (``start_new_session=True``), so its
    pid is also the process-group id.  Killing only the bash wrapper would
    leave pre-commit / hook processes holding the pipes (and the pre-commit
    store lock) alive.
    """
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except (ProcessLookupError, PermissionError):
            pass


def _run_streaming(cmd: list[str], *, cwd: str, env: dict[str, str],
                   timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run *cmd* while echoing stdout/stderr live; capture them as usual.

    ``subprocess.run(capture_output=True)`` shows nothing until the child
    exits, so a hung pre-commit is indistinguishable from a dead job — run
    39653196015 sat on ``format.sh`` for 17 hours with only the workflow
    heartbeat visible.  Two reader threads keep the log live and must drain
    both pipes: reading one synchronously can deadlock once the other pipe's
    buffer fills.

    With *timeout* set, the whole process group is SIGKILLed when it
    expires and the result carries ``FORMAT_TIMEOUT_RC``.  A bare
    ``proc.wait()`` let five nightly runs (10-19..10-23) hang for 6-17
    hours each, so the caller must always bound this.  If the child is
    stuck in uninterruptible
    I/O (a dead NFS mount / pre-commit store lock), SIGKILL cannot reap it;
    the reader threads are daemons and ``join`` is bounded so the flow
    reports the timeout instead of hanging with it.
    """
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env, text=True, bufsize=1,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        start_new_session=True,
    )
    stdout, stderr = proc.stdout, proc.stderr
    if stdout is None or stderr is None:  # pragma: no cover - PIPE guarantees
        raise RuntimeError("subprocess pipes were not created")
    out: list[str] = []
    err: list[str] = []
    readers = [
        threading.Thread(target=_echo_pipe, args=(stdout, out), daemon=True),
        threading.Thread(target=_echo_pipe, args=(stderr, err, "[stderr] "),
                         daemon=True),
    ]
    for reader in readers:
        reader.start()
    try:
        try:
            rc = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            ts_print(f"[format] TIMEOUT after {timeout}s — killing process "
                     f"group (pid {proc.pid})")
            _kill_process_group(proc)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                ts_print("[format] WARNING: process survived SIGKILL "
                         "(uninterruptible I/O?); abandoning it")
            rc = FORMAT_TIMEOUT_RC
            err.append(f"\n[format] timed out after {timeout}s; "
                       f"process group killed\n")
    except BaseException:
        # The flow itself is being cancelled (KeyboardInterrupt) or errored;
        # don't leave pre-commit alive holding the store lock / pipes.
        _kill_process_group(proc)
        raise
    for reader in readers:
        reader.join(timeout=5)
    return subprocess.CompletedProcess(cmd, rc, "".join(out), "".join(err))


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

    Output is echoed line-by-line while pre-commit runs (see
    :func:`_run_streaming`), so the job page shows the current hook instead
    of freezing on the "output begin" header.  The run is capped by
    ``MAIN2MAIN_FORMAT_TIMEOUT`` seconds (default 1800, 0 disables) and the
    whole process group is killed on expiry; timeouts return
    ``FORMAT_TIMEOUT_RC`` instead of blocking forever.

    After format.sh, removes the ``gitleaks`` binary that ``gitleaks.sh``
    downloads to the repo root when the system has no gitleaks in PATH.
    This 22MB binary is a tool artifact, not an adaptation change - leaving
    it would pollute ``git add -N`` / ``git diff`` / ``git add -A`` and end
    up in the PR.
    """
    fmt_script = repo / "format.sh"
    env = os.environ.copy()
    # The -a3-16- segment keeps this branch's cache on its own slice of the
    # persistent /root/.cache volume, never reusing environments left there
    # by other main2main branches on the same runner.
    env["PRE_COMMIT_HOME"] = str(
        Path.home() / ".cache"
        / f"main2main-pre-commit-a3-16-{platform.machine()}")
    # The gitleaks hook can never pass in the a3 container: the image has no
    # system gitleaks and gitleaks.sh falls back to the OBS binary, which is
    # an amd64 build that exits 126 (Exec format error) on aarch64 — even a
    # fresh download (run 33887181257).  Skipping the hook keeps every real
    # lint hook running while removing the permanent format.sh failure.
    env["SKIP"] = "gitleaks-offline-scan"
    # pre-commit is Python: with stdout on a pipe it block-buffers, so
    # without this the "streaming" would still arrive in 8KB bursts.
    env["PYTHONUNBUFFERED"] = "1"
    # Hard cap: five nightly runs (10-19..10-23) hung inside pre-commit for
    # 6-17 h with no output; the workflow's 48 h job timeout was the only
    # bound.  MAIN2MAIN_FORMAT_TIMEOUT=0 disables the cap for debugging.
    raw_timeout = os.environ.get("MAIN2MAIN_FORMAT_TIMEOUT", "").strip()
    try:
        timeout = int(raw_timeout) if raw_timeout else _DEFAULT_FORMAT_TIMEOUT_S
    except ValueError:
        ts_print(f"[format] ignoring invalid MAIN2MAIN_FORMAT_TIMEOUT="
                 f"{raw_timeout!r}; using {_DEFAULT_FORMAT_TIMEOUT_S}s")
        timeout = _DEFAULT_FORMAT_TIMEOUT_S
    r = _run_streaming(
        ["bash", str(fmt_script), "ci"], cwd=str(repo), env=env,
        timeout=timeout if timeout > 0 else None,
    )
    gitleaks_bin = repo / "gitleaks"
    if gitleaks_bin.exists():
        try:
            gitleaks_bin.unlink()
            ts_print("[format] removed downloaded gitleaks binary (tool artifact, not code)")
        except OSError:
            pass
    return r


# The image's default index (tuna) occasionally 403s aarch64 wheels under
# load (run 33897770317: numpy 1.26.4 cp312 aarch64) — one retry on aliyun
# keeps the venv usable instead of degrading to system numpy 2.x, which
# fails pre_ci for every later attempt.
_PIP_FALLBACK_INDEX = "https://mirrors.aliyun.com/pypi/simple/"


def pip_install_with_fallback(python: Path,
                              args: list[str]) -> subprocess.CompletedProcess:
    cmd = [str(python), "-m", "pip", "install", *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if r.returncode != 0:
        r = subprocess.run([*cmd, "-i", _PIP_FALLBACK_INDEX],
                           capture_output=True, text=True, timeout=180)
    return r


# Generated-artifact dirs that must never enter the gate's checks or the PR
# diff — tool output, not business code.  torch.compile dumps
# ``torch_compile_debug/`` into the repo root when a2 UTs run (e.g.
# test_gdn_layerwise_kv.py); without isolation the artifacts get staged and
# the next gate round fails format on them (run 31563761175 round 3).
# vllm-ascend's Runtime Guard writes its config under ``<cwd>/runtime/``,
# and the e2e pytest process runs with cwd = the repo root, so every round
# drops runtime_config.json(+.lock) into the tree.  Run 38705391440: those
# files reached the index, adapter-qa raised them as a blocking
# temp-artifact finding, and the adapter "fixed" it by adding a pattern to
# the repo's own .gitignore — a file no adaptation may touch.  Entries stay
# unanchored on purpose: this same tuple is a ``git ls-files`` pathspec
# below, where a leading "/" is rejected as an invalid path.
GENERATED_ARTIFACT_DIRS = ("torch_compile_debug/", "runtime/")


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
