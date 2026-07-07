#!/usr/bin/env python3
"""Run main2main tests with resource-aware parallel scheduling.

Runs pytest on individual test files (matching vllm-ascend's PR CI pattern),
scheduling them into rounds based on NPU card requirements inferred from the
test path (e.g. one_card → 1, two_card → 2, four_card → 4).

Both local and remote executions parallelize tests across the available NPU
cards within each round: a one_card test takes 1 card, so up to ``total_cards``
of them run concurrently; a two_card test takes 2, so two can share a 4-card
runner. Each test gets its own ``ASCEND_RT_VISIBLE_DEVICES`` so processes do not
collide on the same device. Pass ``--sequential`` to force one-test-per-round.

Execution targets:
  - Local:       run directly (no --remote); --in-place runs against the
                 flow-managed working trees without fetch/reset/patch
  - Remote host: --remote user@host (container from env)
  - Remote env:  --remote env (reads MAIN2MAIN_REMOTE_HOST / _CONTAINER)

When test selection yields nothing, MAIN2MAIN_SMOKE_TESTS (space-separated
test paths) is used as a floor; if still empty the run is recorded as skipped
(tests_skipped=True in the result JSON). CN mirror setup only happens when
MAIN2MAIN_USE_CN_MIRRORS=true.

Usage:
  python3 run_tests.py --vllm-path /workspace/vllm --vllm-commit abc1234 \\
      --ascend-path /workspace/vllm-ascend --ascend-commit def5678 \\
      --step-id step-0 --select-by-files vllm_ascend/worker/worker.py
  python3 run_tests.py ... --remote env --dry-run
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from main2main_flow.utils import ts_print

PASS_RESULTS = {"passed", "env_flake_pass"}
DEFAULT_VLLM_REPO = "https://github.com/vllm-project/vllm.git"
DEFAULT_ASCEND_REPO = "https://github.com/vllm-project/vllm-ascend.git"

def _ssh_opts() -> list[str]:
    # computed at call time so the env var can be set after import
    return ["-o", f"StrictHostKeyChecking={os.getenv('MAIN2MAIN_SSH_STRICT', 'accept-new')}"]

# ---- test path → cards ----

_CARD_PATTERNS: list[tuple[str, int]] = [
    ("one_card", 1), ("singlecard", 1), ("single_card", 1),
    ("two_card", 2), ("2.cards", 2), ("2-card", 2),
    ("four_card", 4), ("4.cards", 4), ("4-card", 4),
    ("eight_card", 8), ("8.cards", 8), ("8-card", 8),
    ("multi.node", 8),
]


def _test_cards(test_path: str) -> int:
    """Infer required NPU cards from the test file path."""
    lower = test_path.lower()
    for pattern, cards in _CARD_PATTERNS:
        if pattern in lower:
            return cards
    return 1


# =============================================================================
# scheduling
# =============================================================================

def _schedule_rounds(tests: list[str], total_cards: int) -> list[list[str]]:
    ordered = sorted(tests, key=lambda t: (-_test_cards(t), t))
    rounds: list[list[str]] = []
    usage: list[int] = []
    for t in ordered:
        need = _test_cards(t)
        if need > total_cards:
            raise ValueError(f"Test '{t}' requires {need} cards but only {total_cards} available")
        for i in range(len(rounds)):
            if usage[i] + need <= total_cards:
                rounds[i].append(t)
                usage[i] += need
                break
        else:
            rounds.append([t])
            usage.append(need)
    return rounds


def _assign_devices(rounds: list[list[str]],
                    phy_ids: list[int] | None = None) -> list[list[tuple[str, str]]]:
    if phy_ids is None:
        max_round = max(sum(_test_cards(t) for t in rnd) for rnd in rounds) if rounds else 0
        phy_ids = list(range(max_round))
    result: list[list[tuple[str, str]]] = []
    for rnd in rounds:
        assigned: list[tuple[str, str]] = []
        offset = 0
        for test in rnd:
            need = _test_cards(test)
            devices = ",".join(str(phy_ids[offset + i]) for i in range(need))
            offset += need
            assigned.append((test, devices))
        result.append(assigned)
    return result


# =============================================================================
# remote helpers
# =============================================================================

def _resolve_remote(remote: str) -> tuple[str, str]:
    host = remote if "@" in remote else os.getenv("MAIN2MAIN_REMOTE_HOST", "")
    container = os.getenv("MAIN2MAIN_REMOTE_CONTAINER", "")
    if not host or not container:
        raise RuntimeError("--remote used but MAIN2MAIN_REMOTE_HOST / _CONTAINER not set")
    ts_print(f"  Remote target: {host}  container: {container}")
    return host, container


def _ssh(host: str, cmd: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh", *_ssh_opts(), host, cmd], **kwargs)


def _ensure_container_running(host: str, container: str) -> None:
    cq = shlex.quote(container)
    check = _ssh(host, f"docker inspect -f '{{{{.State.Running}}}}' {cq}",
                 capture_output=True, text=True)
    if check.returncode != 0:
        ts_print(f"  Container {container} not found on {host}, will try to proceed anyway")
        return
    if check.stdout.strip() == "true":
        ts_print(f"  Container {container} is already running")
        return

    ts_print(f"  Container {container} is stopped, starting ...", flush=True)
    _ssh(host, f"docker start {cq}", capture_output=True, text=True)
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        time.sleep(3)
        poll = _ssh(host, f"docker inspect -f '{{{{.State.Running}}}}' {cq}",
                    capture_output=True, text=True)
        if poll.stdout.strip() == "true":
            ts_print(f"  Container {container} is now running", flush=True)
            return
    ts_print(f"  [warn] Container {container} did not become running within 60s", flush=True)


# =============================================================================
# NPU auto-detection
# =============================================================================

def _detect_cards(run_cmd) -> tuple[int, str]:
    visible = os.environ.get("ASCEND_RT_VISIBLE_DEVICES", "")
    if visible:
        ids = [s.strip() for s in visible.split(",") if s.strip().isdigit()]
        if ids:
            return len(ids), ",".join(ids)
    result = run_cmd(
        "ls /dev/davinci[0-9]* 2>/dev/null | sed 's/.*davinci//' | sort -n"
    )
    ids = []
    for token in result.stdout.strip().split():
        try:
            ids.append(str(int(token)))
        except ValueError:
            pass
    return len(ids), ",".join(ids) if ids else "unknown"


# =============================================================================
# local env setup
# =============================================================================

def _run_checked(cmd: list[str], cwd: Path, label: str) -> None:
    ts_print(f"  {label} ...", flush=True)
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    captured: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        captured.append(line)
        ts_print(f"    {line}", end="", flush=True)
    rc = proc.wait()
    if rc != 0:
        ts_print(f"  {label} FAILED (exit {rc})", file=sys.stderr, flush=True)
        raise RuntimeError(f"{label} failed (exit {rc})")
    ts_print(f"  {label} OK", flush=True)


def _git_out(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}: {r.stderr.strip()}")
    return r.stdout.strip()


def _ensure_repo(path: Path, remote_url: str) -> bool:
    if path.exists():
        if (path / ".git").exists():
            _run_checked(["git", "fetch", "--tags", "--force"], path, "fetch")
            return False
        if any(path.iterdir()):
            raise RuntimeError(
                f"{path} exists but is not a git repo — refusing to delete it; "
                "remove it manually or point at a different path")
        # empty directory: safe to clone into
    path.parent.mkdir(parents=True, exist_ok=True)
    _run_checked(["git", "clone", remote_url, str(path)], path.parent, "clone")
    return True


def _use_cn_mirrors() -> bool:
    return os.getenv("MAIN2MAIN_USE_CN_MIRRORS", "false").lower() == "true"


_MIRROR_CMDS: list[str] = [
    "pip config set global.index-url https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple",
]


def _setup_mirrors() -> None:
    if not _use_cn_mirrors():
        return
    for cmd in _MIRROR_CMDS:
        subprocess.run(["sh", "-c", cmd], capture_output=True, text=True)


def _pip_install(repo_path: Path, extra_env: dict | None = None,
                 requirements: str | None = None, verbose: bool = False,
                 skip_editable: bool = False) -> None:
    if os.environ.get("SKIP_PIP_INSTALL", "false").lower() == "true":
        ts_print(f"  SKIP_PIP_INSTALL=true, skipping pip install for {repo_path.name}")
        return
    # uv reads UV_INDEX_URL / UV_EXTRA_INDEX_URL / UV_INDEX_STRATEGY etc. from the
    # environment (set by schedule_main2main.yaml). pip fallback uses pip.conf set
    # by _setup_mirrors (only when MAIN2MAIN_USE_CN_MIRRORS=true). Only forward
    # caller-provided extra_env (e.g. VLLM_TARGET_DEVICE).
    env_prefix = ""
    if extra_env:
        env_prefix = " ".join(f"{k}={v}" for k, v in extra_env.items()) + " "
    vflag = "-v " if verbose else ""

    use_uv = shutil.which("uv") is not None
    if use_uv:
        installer = "uv pip install"
        label_prefix = "uv pip install"
    else:
        installer = "pip install"
        label_prefix = "pip install"

    cmds = []
    if requirements:
        cmds.append(f"{installer} -r {shlex.quote(requirements)}")
    if not skip_editable:
        cmds.append(f"{installer} {vflag}.")
    for i, cmd in enumerate(cmds):
        _run_checked(["sh", "-c", f"cd {shlex.quote(str(repo_path))} && {env_prefix}{cmd}"],
                     repo_path, f"{label_prefix} ({repo_path.name}) [{i+1}/{len(cmds)}]")


def setup_env(vllm_path: Path, vllm_commit: str, ascend_path: Path,
              ascend_commit: str, patch_path: Path | None = None,
              vllm_remote: str = DEFAULT_VLLM_REPO,
              ascend_remote: str = DEFAULT_ASCEND_REPO,
              in_place: bool = False) -> None:
    _setup_mirrors()
    ts_print("=== Setup vLLM ===")
    if not in_place:
        _ensure_repo(vllm_path, vllm_remote)
    # checkout of a commit detaches HEAD; branch pointers are never moved
    _run_checked(["git", "checkout", vllm_commit], vllm_path, f"checkout {vllm_commit[:8]}")
    ts_print("=== Install vLLM ===")
    _pip_install(vllm_path, extra_env={"VLLM_TARGET_DEVICE": "empty"})

    if in_place:
        # Flow-managed local checkout: the working tree already carries the
        # adaptation, so only verify we are on the expected commit.
        ts_print("=== vllm-ascend (in-place): verify checkout ===")
        head = _git_out(ascend_path, "rev-parse", "HEAD")
        expected = _git_out(ascend_path, "rev-parse", f"{ascend_commit}^{{commit}}")
        if head != expected:
            raise RuntimeError(
                f"vllm-ascend at {ascend_path} is at {head[:12]}, expected "
                f"{expected[:12]} — refusing to run in place")
        ts_print("=== Install vllm-ascend ===")
        _pip_install(ascend_path, requirements="requirements-dev.txt", verbose=True, skip_editable=True)
    elif os.getenv("MAIN2MAIN_KEEP_BRANCH", "false").lower() == "true":
        ts_print("=== vllm-ascend: branch kept, no reset needed ===")
    else:
        ts_print("=== Setup vllm-ascend ===")
        _ensure_repo(ascend_path, ascend_remote)
        status = _git_out(ascend_path, "status", "--porcelain")
        if status:
            excerpt = "\n".join(status.splitlines()[:15])
            raise RuntimeError(
                f"vllm-ascend tree at {ascend_path} is dirty — commit/stash first:\n{excerpt}")
        _run_checked(["git", "fetch", "origin", "--force"], ascend_path, "fetch origin")
        _run_checked(["git", "checkout", "--detach", ascend_commit], ascend_path,
                     f"checkout --detach {ascend_commit[:8]}")
        if patch_path:
            if not patch_path.exists():
                raise RuntimeError(f"patch not found: {patch_path}")
            _run_checked(["git", "apply", str(patch_path)], ascend_path, f"git apply {patch_path.name}")
        ts_print("=== Install vllm-ascend ===")
        _pip_install(ascend_path, requirements="requirements-dev.txt", verbose=True, skip_editable=True)
    ts_print(f"\nSetup complete.\n  vLLM: {vllm_path} @ {vllm_commit[:8]}\n"
          f"  vllm-ascend: {ascend_path} @ {ascend_commit[:8]}"
          + (f" + {patch_path.name}" if patch_path and not in_place else ""))


# =============================================================================
# remote setup script (shell, executed via docker exec)
# =============================================================================

_SHELL_ENSURE_REPO = r'''
# --- {name}: ensure repo ---
if [ -d {path} ] && [ -d {path}/.git ]; then
    echo "  {path} already exists, fetching ..."
    cd {path} && git fetch --tags --force || exit 1
elif [ -d {path} ]; then
    echo "  {path} exists but is not a git repo, removing ..."
    rm -rf {path}
    echo "  Cloning {remote} -> {path} ..."
    mkdir -p $(dirname {path}) && git clone {remote} {path} || exit 1
else
    echo "  Cloning {remote} -> {path} ..."
    mkdir -p $(dirname {path}) && git clone {remote} {path} || exit 1
fi
'''

_SHELL_SETUP = r'''#!/bin/sh
set -e
{mirror_block}
echo "=== Setup vLLM ==="
{ensure_vllm}
echo "  checkout {vllm_commit_short} ..."
cd {vp} && git checkout {vc} || exit 1

echo "=== Install vLLM ==="
cd {vp} && VLLM_TARGET_DEVICE=empty pip install -e . || exit 1

echo "=== Setup vllm-ascend ==="
{ensure_ascend}
echo "  fetch origin && checkout {ascend_commit_short} (detached) ..."
cd {ap} && git fetch origin --force && git checkout -f --detach {ac} || exit 1
{patch_block}
echo "=== Install vllm-ascend ==="
cd {ap} && {ascend_pip_install} || exit 1

echo ""
echo "Setup complete."
echo "  vLLM:        {vllm_path} @ {vllm_commit_short}"
echo "  vllm-ascend: {ascend_path} @ {ascend_commit_short}{patch_suffix}"
'''

_ASCEND_PIP_MIRRORS = ("pip install --extra-index-url https://download.pytorch.org/whl/cpu/ "
                       "--extra-index-url https://mirrors.huaweicloud.com/ascend/repos/pypi "
                       "-r requirements-dev.txt")


def _build_setup_script(vllm_path: Path, vllm_commit: str, ascend_path: Path,
                        ascend_commit: str, patch_path: Path | None = None,
                        vllm_remote: str = DEFAULT_VLLM_REPO,
                        ascend_remote: str = DEFAULT_ASCEND_REPO) -> str:
    patch_block = ""
    patch_suffix = ""
    if patch_path:
        pp = shlex.quote(str(patch_path))
        patch_block = f'echo "  Applying patch {patch_path.name} ..."\ncd {shlex.quote(str(ascend_path))} && git apply {pp} || exit 1'
        patch_suffix = f" + {patch_path.name}"

    if _use_cn_mirrors():
        mirror_block = 'echo "=== Setup mirrors ==="\n' + "\n".join(_MIRROR_CMDS) + "\n"
        ascend_pip_install = _ASCEND_PIP_MIRRORS
    else:
        mirror_block = ""
        ascend_pip_install = "pip install -r requirements-dev.txt"

    return _SHELL_SETUP.format(
        vp=shlex.quote(str(vllm_path)),
        ap=shlex.quote(str(ascend_path)),
        vc=shlex.quote(vllm_commit[:8]),
        ac=shlex.quote(ascend_commit[:8]),
        vllm_path=str(vllm_path), ascend_path=str(ascend_path),
        vllm_commit_short=vllm_commit[:8], ascend_commit_short=ascend_commit[:8],
        mirror_block=mirror_block,
        ascend_pip_install=ascend_pip_install,
        ensure_vllm=_SHELL_ENSURE_REPO.format(name="vllm", path=shlex.quote(str(vllm_path)),
                                               remote=shlex.quote(vllm_remote)),
        ensure_ascend=_SHELL_ENSURE_REPO.format(name="ascend", path=shlex.quote(str(ascend_path)),
                                                 remote=shlex.quote(ascend_remote)),
        patch_block=patch_block, patch_suffix=patch_suffix,
    )


# =============================================================================
# test execution helpers
# =============================================================================

def _run_to_log(command: list[str], cwd: Path, log_path: Path,
                env: dict[str, str]) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as f:
        proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, bufsize=1)
        assert proc.stdout is not None
        for line in proc.stdout:
            f.write(line)
            ts_print(line, end="", flush=True)
        return proc.wait()


def _run_summary(ci_log_summary: Path, log_path: Path, summary_path: Path,
                 step_id: str | int, round_number: int) -> dict:
    if not ci_log_summary.exists():
        return {"summary_exit_code": 1,
                "summary_error": f"ci_log_summary.py not found: {ci_log_summary}",
                "summary": None}
    r = subprocess.run([sys.executable, str(ci_log_summary), "--log-file", str(log_path),
                        "--format", "llm-json", "--output", str(summary_path),
                        "--step-name", f"main2main {step_id} round {round_number}"],
                       text=True, capture_output=True, check=False)
    if r.returncode != 0:
        return {"summary_exit_code": r.returncode,
                "summary_error": r.stderr.strip() or r.stdout.strip(), "summary": None}
    if not summary_path.exists() or summary_path.stat().st_size == 0:
        return {"summary_exit_code": r.returncode,
                "summary_error": f"summary output was not written: {summary_path}",
                "summary": None}
    try:
        return {"summary_exit_code": r.returncode, "summary_error": None,
                "summary": json.loads(summary_path.read_text(encoding="utf-8"))}
    except json.JSONDecodeError as exc:
        return {"summary_exit_code": r.returncode,
                "summary_error": f"invalid summary JSON: {exc}", "summary": None}


def _count(summary: dict | None, field: str) -> int:
    if not summary:
        return 0
    count_field = f"{field}_count"
    if count_field in summary:
        return int(summary[count_field])
    value = summary.get(field, [])
    return len(value) if isinstance(value, list) else 0


def _classify(exit_code: int, summary: dict | None, error: str | None) -> str:
    if exit_code == 0:
        return "passed"
    if error or summary is None:
        return "summary_error"
    if len(summary.get("code_bugs", [])) == 0 and len(summary.get("env_flakes", [])) > 0:
        return "env_flake_pass"
    return "failed"


def _discover_test_files(ascend_path: Path, paths: list[str]) -> list[str]:
    """Expand directories into individual test_*.py files."""
    result: list[str] = []
    for p in paths:
        full = (ascend_path / p).resolve() if not os.path.isabs(p) else Path(p).resolve()
        if full.is_file():
            result.append(str(full.relative_to(ascend_path)))
        elif full.is_dir():
            for tf in sorted(full.rglob("test_*.py")):
                result.append(str(tf.relative_to(ascend_path)))
        else:
            ts_print(f"  [warn] Test path not found: {p}", flush=True)
    return result


def _select_tests_by_files(ascend_path: Path, changed_files: list[str]) -> list[str] | None:
    """Call vllm-ascend's select_tests.py to resolve changed files → test files.

    Returns a list of test file paths, or None if the selector is unavailable.
    """
    select_script = ascend_path / ".github/workflows/scripts/select_tests.py"
    if not select_script.exists():
        ts_print("  [warn] select_tests.py not found, falling back to full scan", flush=True)
        return None

    r = subprocess.run(
        [sys.executable, str(select_script), "--changed-files"] + changed_files,
        cwd=ascend_path, capture_output=True, text=True,
        env={**os.environ, "GITHUB_OUTPUT": ""},  # force stdout output
    )
    if r.stderr.strip():
        for line in r.stderr.strip().splitlines():
            ts_print(f"  [select_tests] {line}", flush=True)
    if r.returncode != 0:
        ts_print(f"  [warn] select_tests.py failed (exit {r.returncode})", flush=True)
        return None

    # Parse key=value output (GITHUB_OUTPUT format)
    test_groups_json = ""
    for line in r.stdout.strip().splitlines():
        if line.startswith("test_groups="):
            test_groups_json = line[len("test_groups="):]
            break

    if not test_groups_json:
        return None

    try:
        groups = json.loads(test_groups_json)
    except json.JSONDecodeError:
        return None

    tests: list[str] = []
    for g in groups:
        if g.get("npu_type") == "cpu":
            continue  # skip CPU-only tests, main2main runs on NPU
        for t in g.get("tests", "").split():
            tests.append(t)
    return tests or None


def _build_test_cmd(test: str, devices: str, *,
                    ascend_path: Path,
                    remote_host: str | None,
                    remote_container: str | None,
                    remote_ascend: Path,
                    mock: bool, mock_scale: float = 0.1,
                    s_env: dict[str, str]) -> list[str]:
    """Build the command to run a single pytest target."""
    if mock:
        duration = int(max(_test_cards(test) * 120, 30) * mock_scale)
        if remote_host:
            return ["ssh", *_ssh_opts(), remote_host,
                    f"docker exec {remote_container} sleep {duration}"]
        return ["sleep", str(duration)]

    if remote_host:
        env_vars = [f"ASCEND_RT_VISIBLE_DEVICES={devices}"]
        for k in sorted(s_env):
            if k.startswith("VLLM_"):
                env_vars.append(f"{k}={shlex.quote(s_env[k])}")
        inner = (
            f"docker exec -w {shlex.quote(str(remote_ascend))} {remote_container} "
            f"env {' '.join(env_vars)} "
            f"pytest -sv --color=yes {shlex.quote(test)}"
        )
        return ["ssh", *_ssh_opts(), remote_host, inner]
    return [sys.executable, "-m", "pytest", "-sv", "--color=yes", test]


def _run_one_test(cmd: list[str], log_path: Path, summary_path: Path,
                  test: str, devices: str, ci_log_summary: Path,
                  ascend_path: Path, step_id: str | int, round_number: int,
                  env: dict[str, str], *, is_remote: bool, is_mock: bool) -> dict:
    """Execute one test and return its result dict."""
    cwd = Path("/tmp") if is_remote else ascend_path
    if not is_remote and not is_mock:
        env["ASCEND_RT_VISIBLE_DEVICES"] = devices
    exit_code = _run_to_log(cmd, cwd, log_path, env)
    cards = _test_cards(test)

    if is_mock:
        return {"test": test, "cards_required": cards,
                "run_suite_exit_code": exit_code,
                "ci_result": "passed" if exit_code == 0 else "failed",
                "summary_error": None, "code_bugs_count": 0, "env_flakes_count": 0,
                "failed_test_files_count": 0, "failed_test_cases_count": 0,
                "log_path": str(log_path), "summary_path": str(summary_path)}

    sr = _run_summary(ci_log_summary, log_path, summary_path, step_id, round_number)
    s, se = sr["summary"], sr["summary_error"]
    return {"test": test, "cards_required": cards,
            "run_suite_exit_code": exit_code,
            "ci_result": _classify(exit_code, s, se),
            "summary_error": se,
            "code_bugs_count": len((s or {}).get("code_bugs", [])),
            "env_flakes_count": len((s or {}).get("env_flakes", [])),
            "failed_test_files_count": _count(s, "failed_test_files"),
            "failed_test_cases_count": _count(s, "failed_test_cases"),
            "log_path": str(log_path), "summary_path": str(summary_path)}


# =============================================================================
# main entry point
# =============================================================================

def run_tests(
    vllm_path: str | Path,
    vllm_commit: str,
    ascend_path: str | Path,
    ascend_commit: str,
    patch_path: str | Path | None = None,
    step_id: str | int = 0,
    select_by_files: list[str] | None = None,
    test_cases: list[str] | None = None,
    remote: str | None = None,
    log_dir: str | Path = "",
    remote_vllm_path: str | Path = "/vllm-workspace/vllm",
    remote_ascend_path: str | Path = "/vllm-workspace/vllm-ascend",
    round_number: int = 1,
    dry_run: bool = False,
    sequential: bool = False,
    mock: bool = False,
    mock_scale: float = 0.1,
    in_place: bool = False,
) -> dict:
    """Run end-to-end tests for a main2main step.

    Args:
        select_by_files: Changed file paths for precise test selection
                         via vllm-ascend's select_tests.py.
        in_place: Run against the flow-managed local working trees: no
                  fetch/reset/patch on vllm-ascend, only a commit check.

    Returns a dict that always contains "result_path" (the written
    round-{n}-result.json) and "tests_skipped".
    """
    vllm_path = Path(vllm_path)
    ascend_path = Path(ascend_path)
    if patch_path:
        patch_path = Path(patch_path)
    log_dir = Path(log_dir)
    remote_vllm = Path(remote_vllm_path) if remote_vllm_path else Path("/vllm-workspace/vllm")
    remote_ascend = Path(remote_ascend_path) if remote_ascend_path else Path("/vllm-workspace/vllm-ascend")
    ci_dir = log_dir / str(step_id) / "tests"
    result_path = (ci_dir / f"round-{round_number}-result.json").resolve()

    # ---- step 1: resolve tests ----
    if test_cases:
        test_files = test_cases
        ts_print(f"Using {len(test_files)} fixed test cases")
    elif select_by_files:
        ts_print(f"Selecting tests for {len(select_by_files)} changed file(s)")
        test_files = _select_tests_by_files(ascend_path, select_by_files) or []
        ts_print(f"Selected {len(test_files)} test(s)")
    else:
        test_files = []

    if not test_files:
        smoke = os.getenv("MAIN2MAIN_SMOKE_TESTS", "").split()
        if smoke:
            ts_print(f"[WARN] no tests selected — falling back to MAIN2MAIN_SMOKE_TESTS "
                     f"({len(smoke)} path(s))", flush=True)
            test_files = _discover_test_files(ascend_path, smoke)

    if not test_files:
        ts_print("[WARN] no tests selected and no smoke tests available — step passes "
                 "WITHOUT runtime validation; set MAIN2MAIN_SMOKE_TESTS", flush=True)
        result = {
            "step_id": step_id, "round": round_number,
            "ci_result": "skipped", "passed": True,
            "can_commit": True, "requires_fix": False,
            "tests_skipped": True, "tests": [], "suite_results": {},
            "result_path": str(result_path),
        }
        ci_dir.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                               encoding="utf-8")
        return result

    # ---- step 2: resolve remote ----
    remote_host: str | None = None
    remote_container: str | None = None
    if remote:
        remote_host, remote_container = _resolve_remote(remote)
        _ensure_container_running(remote_host, remote_container)

    # ---- step 2.5: auto-detect cards ----
    if remote_host and remote_container:
        cq = shlex.quote(remote_container)
        run_cmd = lambda cmd: _ssh(remote_host, f"docker exec {cq} sh -c {shlex.quote(cmd)}",
                                   capture_output=True, text=True)
    else:
        run_cmd = lambda cmd: subprocess.run(["sh", "-c", cmd], capture_output=True, text=True)

    total_cards, phy_ids = _detect_cards(run_cmd)
    label = "on remote" if remote_host else "local"
    ts_print(f"  Auto-detected {total_cards} NPU(s) {label} (Phy-IDs: {phy_ids})")
    if total_cards <= 0:
        raise RuntimeError("could not detect any NPU cards")
    all_phy_ids = [int(x) for x in phy_ids.split(",")]

    # ---- step 3: sync patch ----
    if patch_path and remote_host:
        local = patch_path.resolve()
        if not local.exists():
            local = Path.cwd() / str(patch_path).lstrip("/")
        if local.exists():
            ts_print(f"=== Syncing patch: {local} -> {remote_container}:{patch_path} ===")
            _ssh(remote_host, f"docker exec {remote_container} mkdir -p {shlex.quote(str(patch_path.parent))}",
                 capture_output=True, text=True, check=True)
            with open(local, "rb") as f:
                _ssh(remote_host, f"docker exec -i {remote_container} sh -c 'cat > {shlex.quote(str(patch_path))}'",
                     stdin=f, capture_output=True, text=False, check=True)
            ts_print("  Patch synced to container successfully")

    # ---- step 4: setup repos ----
    if remote_host:
        ts_print("=== Running setup on remote container ===")
        script = _build_setup_script(remote_vllm, vllm_commit, remote_ascend, ascend_commit, patch_path)
        inner = f"docker exec {remote_container} sh -c {shlex.quote(script)}"
        proc = subprocess.Popen(["ssh", *_ssh_opts(), remote_host, inner],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        assert proc.stdout is not None
        for line in proc.stdout:
            ts_print(line, end="", flush=True)
        if proc.wait() != 0:
            raise RuntimeError(f"Remote setup failed with exit code {proc.returncode}")
    else:
        setup_env(vllm_path, vllm_commit, ascend_path, ascend_commit, patch_path,
                  in_place=in_place)

    # ---- step 5: locate ci_log_summary ----
    ci_log_summary = Path(__file__).parent / "ci_log_summary.py"

    # ---- step 6: env ----
    env = os.environ.copy()
    env.setdefault("VLLM_WORKER_MULTIPROC_METHOD", "spawn")
    env.setdefault("VLLM_USE_MODELSCOPE", "true")

    # ---- step 7: schedule ----
    rounds = [[t] for t in test_files] if sequential else _schedule_rounds(test_files, total_cards)
    device_rounds = _assign_devices(rounds, all_phy_ids)

    parallel_count = sum(1 for r in rounds if len(r) > 1)
    ts_print(f"Schedule ({len(rounds)} round(s), {parallel_count} parallel, total cards: {total_cards}):")
    for i, rnd in enumerate(device_rounds):
        usage = sum(_test_cards(t) for t, _ in rnd)
        mode = "parallel" if len(rnd) > 1 else "serial"
        ts_print(f"  Round {i+1} ({mode}, using {usage}/{total_cards} cards):")
        for t, d in rnd:
            ts_print(f"    {t}  ({_test_cards(t)}c, devs={d})")
    ts_print(flush=True)

    if dry_run:
        ts_print("[dry-run] Skipping execution.", flush=True)
        result = {
            "step_id": step_id, "round": round_number,
            "ci_result": "dry_run", "passed": False,
            "can_commit": False, "requires_fix": False,
            "tests_skipped": False,
            "tests": test_files, "suite_results": {},
            "result_path": str(result_path),
        }
        ci_dir.mkdir(parents=True, exist_ok=True)
        result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                               encoding="utf-8")
        return result

    # ---- step 8: execute ----
    t0 = time.monotonic()
    all_results: list[dict] = []
    rounds_info: list[dict] = []

    for round_idx, rnd in enumerate(device_rounds, start=1):
        round_t0 = time.monotonic()
        ts_print(f"\n== Round {round_idx}/{len(rounds)}: {len(rnd)} test(s) ==", flush=True)

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(rnd)) as executor:
            futs = {}
            for test, devices in rnd:
                slug = test.replace("/", "__").replace(".py", "").replace("::", "--")
                lp = ci_dir / f"round-{round_number}-{slug}.log"
                sp = ci_dir / f"round-{round_number}-{slug}-summary.json"
                cmd = _build_test_cmd(test, devices, ascend_path=ascend_path,
                                      remote_host=remote_host, remote_container=remote_container,
                                      remote_ascend=remote_ascend,
                                      mock=mock, mock_scale=mock_scale, s_env=env)
                fut = executor.submit(_run_one_test, cmd, lp, sp, test, devices,
                                      ci_log_summary, ascend_path,
                                      step_id, round_number, env.copy(),
                                      is_remote=bool(remote_host), is_mock=mock)
                futs[fut] = test
                ts_print(f"  [{test}] started ({_test_cards(test)} card(s))", flush=True)

            round_results = []
            printed_failure = False
            for fut in concurrent.futures.as_completed(futs):
                r = fut.result()
                round_results.append(r)
                ts_print(f"  [{futs[fut]}] done: exit={r['run_suite_exit_code']}, "
                      f"result={r['ci_result']}, bugs={r['code_bugs_count']}, "
                      f"flakes={r['env_flakes_count']}", flush=True)
                if not printed_failure and r['run_suite_exit_code'] != 0:
                    printed_failure = True
                    log_path = Path(r['log_path'])
                    if log_path.exists():
                        log_content = log_path.read_text(encoding="utf-8", errors="replace")
                        tail = "\n".join(log_content.splitlines()[-40:])
                        ts_print(f"  [FAILED] log tail ({r['test']}):\n{tail}", flush=True)

        round_elapsed = time.monotonic() - round_t0
        all_results.extend(round_results)
        rounds_info.append({"round": round_idx, "tests": [r["test"] for r in round_results],
                            "cards_used": sum(_test_cards(t) for t, _ in rnd),
                            "total_cards": total_cards, "elapsed_s": round(round_elapsed, 1)})
        ts_print(f"  Round {round_idx} elapsed: {round_elapsed:.1f}s", flush=True)

    total_elapsed = time.monotonic() - t0

    # ---- step 9: aggregate ----
    outcomes = {r["ci_result"] for r in all_results}
    if "failed" in outcomes:
        overall = "failed"
    elif "summary_error" in outcomes:
        overall = "summary_error"
    elif outcomes == {"passed"}:
        overall = "passed"
    else:
        overall = "env_flake_pass"

    result = {
        "step_id": step_id, "round": round_number,
        "label": "+".join(r["test"] for r in all_results),
        "tests": [r["test"] for r in all_results],
        "ci_result": overall, "passed": overall == "passed",
        "can_commit": overall in PASS_RESULTS, "requires_fix": overall == "failed",
        "tests_skipped": False, "result_path": str(result_path),
        "log_path": str(ci_dir), "summary_path": str(ci_dir),
        "total_cards": total_cards, "sequential": sequential, "remote": remote,
        "elapsed_s": round(total_elapsed, 1), "rounds": rounds_info,
        "suite_results": {r["test"]: r for r in all_results},
        "code_bugs_count": sum(r["code_bugs_count"] for r in all_results),
        "env_flakes_count": sum(r["env_flakes_count"] for r in all_results),
        "failed_test_files_count": sum(r["failed_test_files_count"] for r in all_results),
        "failed_test_cases_count": sum(r["failed_test_cases_count"] for r in all_results),
    }
    result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    ts_print(f"\nmain2main CI aggregated: {overall}  (can_commit={result['can_commit']})", flush=True)
    ts_print(f"Total elapsed: {total_elapsed:.1f}s  ({total_elapsed/60:.1f} min)", flush=True)
    ts_print(f"Result written to {result_path}", flush=True)
    return result


# =============================================================================
# CLI
# =============================================================================

def main() -> None:
    p = argparse.ArgumentParser(description="Run main2main CI with resource-aware parallel scheduling.")
    p.add_argument("--vllm-path", type=Path, required=True)
    p.add_argument("--vllm-commit", required=True)
    p.add_argument("--ascend-path", type=Path, required=True)
    p.add_argument("--ascend-commit", required=True)
    p.add_argument("--patch", type=Path)
    p.add_argument("--step-id", type=str, required=True)
    p.add_argument("--round", type=int, default=1)
    p.add_argument("--select-by-files", nargs="*", default=None,
                   help="Changed file paths for precise test selection via select_tests.py.")
    p.add_argument("--log-dir", type=Path, default=Path("."))
    p.add_argument("--sequential", action="store_true")
    p.add_argument("--remote")
    p.add_argument("--remote-vllm-path", type=Path)
    p.add_argument("--remote-ascend-path", type=Path)
    p.add_argument("--in-place", action="store_true",
                   help="Run against the local working trees as-is (no fetch/reset/patch).")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--mock", action="store_true")
    p.add_argument("--mock-scale", type=float, default=0.1)
    args = p.parse_args()

    try:
        result = run_tests(
            vllm_path=args.vllm_path, vllm_commit=args.vllm_commit,
            ascend_path=args.ascend_path, ascend_commit=args.ascend_commit,
            patch_path=args.patch, step_id=args.step_id,
            select_by_files=args.select_by_files,
            remote=args.remote, log_dir=args.log_dir,
            remote_vllm_path=args.remote_vllm_path,
            remote_ascend_path=args.remote_ascend_path,
            round_number=args.round, dry_run=args.dry_run,
            sequential=args.sequential, mock=args.mock, mock_scale=args.mock_scale,
            in_place=args.in_place,
        )
    except (RuntimeError, ValueError) as exc:
        ts_print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)
    sys.exit(0 if result.get("can_commit", False) else 1)


if __name__ == "__main__":
    main()
