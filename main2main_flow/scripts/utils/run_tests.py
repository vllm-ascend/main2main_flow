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
On dual-die NPUs (A3: dies 0-1, 2-3, ... live on one physical card and must
be used together) pass ``--pair-aligned-devices`` — every test then starts on
an even die and odd dies are never assigned to any task.

Execution targets:
  - Local:       run directly (no --remote)
  - Remote host: --remote user@host (container from env)
  - Remote env:  --remote env (reads MAIN2MAIN_REMOTE_HOST / _CONTAINER)

Usage:
  python3 run_tests.py --vllm-path /workspace/vllm --vllm-commit abc1234 \\
      --ascend-path /workspace/vllm-ascend --ascend-commit def5678 \\
      --step-id 0 --total-cards 8 --test tests/e2e/pull_request/light/one_card/test_foo.py
  python3 run_tests.py ... --test tests/e2e/pull_request/light/ --remote env --dry-run
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from main2main_flow.scripts.utils.utils import ts_print

# precision_pass: numeric-precision assertion failures (vllm output vs a
# reference differs slightly under float16, e.g. torch.allclose on the
# 310p classification head).  Treating them as pass was a user decision
# (2026-09-03): NPU precision drift is hard to fix and must not burn
# adapter fix rounds — but it is tracked separately from env flakes.
PASS_RESULTS = {"passed", "env_flake_pass", "precision_pass"}
DEFAULT_VLLM_REPO = "https://github.com/vllm-project/vllm.git"
DEFAULT_ASCEND_REPO = "https://github.com/vllm-project/vllm-ascend.git"

_SSH_OPTS = ["-o", "StrictHostKeyChecking=no"]

# ---- test path → cards ----

_CARD_PATTERNS: list[tuple[str, int]] = [
    ("tests/ut/", 0), ("tests.ut.", 0),
    ("one_card", 1), ("singlecard", 1), ("single_card", 1),
    ("two_card", 2), ("2.cards", 2), ("2-card", 2),
    ("four_card", 4), ("4.cards", 4), ("4-card", 4),
    ("eight_card", 8), ("8.cards", 8), ("8-card", 8),
    ("multi.node", 8),
]

# Per-test card overrides set by run_tests() when callers pass explicit
# card counts (e.g. the external E2E exec: group num_npus is authoritative —
# a "one_card" path routed to 310p overrides to 310p_x4, and NPU-routed
# tests/ut files carry their routing cards, which the path heuristic can't
# know).  Empty dict → pure path heuristic.
_CARD_OVERRIDES: dict[str, int] = {}


def _test_cards(test_path: str) -> int:
    """Infer required NPU cards from the test file path."""
    override = _CARD_OVERRIDES.get(test_path)
    if override is not None:
        return override
    lower = test_path.lower()
    for pattern, cards in _CARD_PATTERNS:
        if pattern in lower:
            return cards
    return 1


# =============================================================================
# scheduling
# =============================================================================

# Default estimated time for tests not listed in test_config.yaml.
_DEFAULT_ESTIMATED_SECONDS = 600


def _load_estimated_times(ascend_path: Path) -> dict[str, int]:
    """Load ``estimated_times`` from vllm-ascend's test_config.yaml.

    Returns a dict mapping test path → seconds.  Node-level entries
    (``file::node``) take priority over file-level entries, and unlisted
    tests default to ``_DEFAULT_ESTIMATED_SECONDS``.
    """
    config_path = ascend_path / ".github/workflows/scripts/test_config.yaml"
    if not config_path.exists():
        return {}
    try:
        import yaml
        with open(config_path) as f:
            config = yaml.safe_load(f)
    except Exception:
        return {}
    raw = config.get("estimated_times", {}) if isinstance(config, dict) else {}
    times: dict[str, int] = {}
    for k, v in raw.items():
        if isinstance(v, (int, float)):
            times[k] = int(v)
    return times


def _lookup_time(test: str, times: dict[str, int]) -> int:
    """Return the estimated seconds for *test*, with node-level priority."""
    return times.get(test) or times.get(test.split("::")[0]) or _DEFAULT_ESTIMATED_SECONDS


_DEVICE_OVERRIDE_PATTERNS = re.compile(
    r"RemoteEPDServer|RemotePDServer|ASCEND_RT_VISIBLE_DEVICES\s*="
)


def _detect_device_overriders(test_files: list[str],
                              ascend_path: Path) -> set[str]:
    """Tests whose child processes hardcode physical devices.

    RemoteEPDServer/RemotePDServer (tests/e2e/conftest.py) override
    ASCEND_RT_VISIBLE_DEVICES to "0"/"1" when spawning their vllm servers,
    ignoring whatever devices the runner assigned (run 31357662108:
    disaggregated_encoder's servers loaded Qwen2.5-VL onto the deepseek
    test's cards, failing its 0.8-utilization memory check).  Such tests must
    own physical devices 0..cards-1, and only one of them may share a round
    (they all fight over the same 0..N-1 range).
    """
    overriders: set[str] = set()
    for t in test_files:
        rel = t.split("::")[0]
        path = ascend_path / rel
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _DEVICE_OVERRIDE_PATTERNS.search(src):
            overriders.add(t)
    return overriders


def _schedule_rounds(tests: list[str], total_cards: int,
                     estimated_times: dict[str, int] | None = None,
                     device_overriders: set[str] | None = None) -> list[list[str]]:
    # Sort by card count descending, then estimated time descending (longest
    # first — a greedy bin-packing heuristic that shortens makespan).
    times = estimated_times or {}
    overriders = device_overriders or set()
    ordered = sorted(tests, key=lambda t: (-_test_cards(t),
                                            -_lookup_time(t, times), t))
    rounds: list[list[str]] = []
    usage: list[int] = []
    for t in ordered:
        need = _test_cards(t)
        if need > total_cards:
            raise ValueError(f"Test '{t}' requires {need} cards but only {total_cards} available")
        if t in overriders:
            # Device-overriding tests each start their own round (they all
            # hardcode physical devices 0..N-1, so a round can hold at most
            # one of them).  As the round's first test it gets 0..N-1, which
            # matches its hardcoded range; other tests fill the cards after.
            rounds.append([t])
            usage.append(need)
            continue
        for i in range(len(rounds)):
            if usage[i] + need <= total_cards:
                rounds[i].append(t)
                usage[i] += need
                break
        else:
            rounds.append([t])
            usage.append(need)
    return rounds


def _validate_pair_aligned(phy_ids: list[int]) -> None:
    """Reject device sets that split a dual-die card's pair across tasks.

    The A3 is a dual-die package: dies 0-1, 2-3, ... live on the same
    physical card and must be used together.  The runner's visible device
    list must therefore be closed under pairing (id ^ 1 present for every
    id) — anything else means the device plugin allocated individual dies
    and no software-side alignment can fix it.
    """
    present = set(phy_ids)
    lone = [i for i in phy_ids if (i ^ 1) not in present]
    if lone:
        raise ValueError(
            "visible devices must form complete dual-die pairs (0-1, 2-3, "
            f"4-5, ...); got {phy_ids} with lone dies {lone}.  The A3 is a "
            "dual-die card — the device plugin must allocate whole pairs."
        )


def _assign_devices(rounds: list[list[str]],
                    phy_ids: list[int] | None = None,
                    pair_aligned: bool = False) -> list[list[tuple[str, str]]]:
    if phy_ids is None:
        max_round = max(sum(_test_cards(t) for t in rnd) for rnd in rounds) if rounds else 0
        phy_ids = list(range(max_round))
    if pair_aligned:
        _validate_pair_aligned(phy_ids)
    result: list[list[tuple[str, str]]] = []
    for rnd in rounds:
        assigned: list[tuple[str, str]] = []
        offset = 0
        for test in rnd:
            if pair_aligned and offset % 2:
                # Never start a test on the odd die of a pair — the lone die
                # stays unused rather than being split across two tasks.
                offset += 1
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
        ts_print("Error: --remote used but MAIN2MAIN_REMOTE_HOST / _CONTAINER not set", file=sys.stderr)
        sys.exit(1)
    ts_print(f"  Remote target: {host}  container: {container}")
    return host, container


def _ssh(host: str, cmd: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(["ssh", *_SSH_OPTS, host, cmd], **kwargs)


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


def _sync_remote_dir(host: str, remote_dir: str, local_dir: Path) -> bool:
    check = _ssh(host, f"test -d {shlex.quote(remote_dir)} && ls -A {shlex.quote(remote_dir)} 2>/dev/null | head -1",
                 capture_output=True, text=True)
    if check.returncode != 0 or not check.stdout.strip():
        return False
    local_dir.mkdir(parents=True, exist_ok=True)
    if shutil.which("rsync"):
        cmd = ["rsync", "-az", "-e", f"ssh {' '.join(_SSH_OPTS)}",
               f"{host}:{remote_dir}/", str(local_dir) + "/"]
    else:
        cmd = ["scp", "-r", *_SSH_OPTS, f"{host}:{remote_dir}/.", str(local_dir) + "/"]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        ts_print(f"  [ERROR] Failed to sync: {result.stderr.strip()}", flush=True)
        return False
    return True


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
        sys.exit(rc)
    ts_print(f"  {label} OK", flush=True)


def _ensure_repo(path: Path, remote_url: str) -> bool:
    if path.exists():
        if not (path / ".git").exists():
            ts_print(f"  {path} exists but is not a git repo, removing ... ", end="", flush=True)
            shutil.rmtree(path)
            ts_print("OK")
        else:
            _run_checked(["git", "fetch", "--tags", "--force"], path, "fetch")
            return False
    path.parent.mkdir(parents=True, exist_ok=True)
    _run_checked(["git", "clone", remote_url, str(path)], path, "clone")
    return True


_MIRROR_CMDS: list[str] = [
    "pip config set global.index-url https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple",
]


def _setup_mirrors() -> None:
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
    # by _setup_mirrors. Only forward caller-provided extra_env (e.g. VLLM_TARGET_DEVICE).
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
              ascend_remote: str = DEFAULT_ASCEND_REPO) -> None:
    _setup_mirrors()
    ts_print("=== Setup vLLM ===")
    _ensure_repo(vllm_path, vllm_remote)
    _run_checked(["git", "checkout", vllm_commit], vllm_path, f"checkout {vllm_commit[:8]}")
    ts_print("=== Install vLLM ===")
    _pip_install(vllm_path, extra_env={"VLLM_TARGET_DEVICE": "empty"})

    if os.getenv("MAIN2MAIN_KEEP_BRANCH", "false").lower() == "true":
        ts_print("=== vllm-ascend: branch kept, no reset needed ===")
    else:
        ts_print("=== Setup vllm-ascend ===")
        _ensure_repo(ascend_path, ascend_remote)
        _run_checked(["git", "fetch", "origin", "--force"], ascend_path, "fetch origin")
        _run_checked(["git", "reset", "--hard", "origin/main"], ascend_path, "reset to origin/main")
        _run_checked(["git", "checkout", ascend_commit], ascend_path, f"checkout {ascend_commit[:8]}")
        if patch_path:
            if not patch_path.exists():
                ts_print(f"Error: patch not found: {patch_path}", file=sys.stderr)
                sys.exit(1)
            _run_checked(["git", "apply", str(patch_path)], ascend_path, f"git apply {patch_path.name}")
        ts_print("=== Install vllm-ascend ===")
        _pip_install(ascend_path, requirements="requirements-dev.txt", verbose=True, skip_editable=True)
    ts_print(f"\nSetup complete.\n  vLLM: {vllm_path} @ {vllm_commit[:8]}\n"
          f"  vllm-ascend: {ascend_path} @ {ascend_commit[:8]}"
          + (f" + {patch_path.name}" if patch_path else ""))


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
echo "=== Setup mirrors ==="
{mirror_cmds}

echo "=== Setup vLLM ==="
{ensure_vllm}
echo "  checkout {vllm_commit_short} ..."
cd {vp} && git checkout {vc} || exit 1

echo "=== Install vLLM ==="
cd {vp} && VLLM_TARGET_DEVICE=empty pip install -e . || exit 1

echo "=== Setup vllm-ascend ==="
{ensure_ascend}
echo "  fetch origin && reset to origin/main ..."
cd {ap} && git fetch origin --force && git reset --hard origin/main || exit 1
echo "  checkout {ascend_commit_short} ..."
cd {ap} && git checkout {ac} || exit 1
{patch_block}
echo "=== Install vllm-ascend ==="
cd {ap} && pip install --extra-index-url https://download.pytorch.org/whl/cpu/ --extra-index-url https://mirrors.huaweicloud.com/ascend/repos/pypi -r requirements-dev.txt || exit 1

echo ""
echo "Setup complete."
echo "  vLLM:        {vllm_path} @ {vllm_commit_short}"
echo "  vllm-ascend: {ascend_path} @ {ascend_commit_short}{patch_suffix}"
'''


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

    return _SHELL_SETUP.format(
        vp=shlex.quote(str(vllm_path)),
        ap=shlex.quote(str(ascend_path)),
        vc=shlex.quote(vllm_commit[:8]),
        ac=shlex.quote(ascend_commit[:8]),
        vllm_path=str(vllm_path), ascend_path=str(ascend_path),
        vllm_commit_short=vllm_commit[:8], ascend_commit_short=ascend_commit[:8],
        mirror_cmds="\n".join(_MIRROR_CMDS),
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
                env: dict[str, str], timeout_s: int | None = None) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # start_new_session=True puts the child (pytest + its vllm workers) in a
    # separate process group so we can kill the entire group on timeout.
    # Without this, a vllm worker that hangs after pytest exits keeps stdout
    # open and `for line in proc.stdout` never sees EOF — the flow hangs
    # forever (see runs 30502548494 and 30645455161, both hung 4-36 hours).
    proc = subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1,
                            close_fds=True, start_new_session=True)
    assert proc.stdout is not None

    # Per-suite timeout (env-configurable, default 30 min); test_policy.json
    # "timeouts" can override it per test.
    if timeout_s is None:
        timeout_s = int(os.environ.get("MAIN2MAIN_TEST_TIMEOUT", "1800"))
    deadline = time.monotonic() + timeout_s

    with log_path.open("w", encoding="utf-8") as f:
        lines_queue: queue.Queue[str | None] = queue.Queue()

        def _reader():
            assert proc.stdout is not None
            for line in proc.stdout:
                lines_queue.put(line)
            lines_queue.put(None)

        reader = threading.Thread(target=_reader, daemon=True)
        reader.start()

        killed = False
        try:
            while True:
                try:
                    line = lines_queue.get(timeout=1.0)
                except queue.Empty:
                    now = time.monotonic()
                    if now > deadline:
                        ts_print(f"\n  [TIMEOUT] suite exceeded {timeout_s}s, killing process group",
                                 flush=True)
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        except (ProcessLookupError, PermissionError):
                            proc.kill()
                        killed = True
                        continue
                    continue
                if line is None:
                    break
                f.write(line)
                ts_print(line, end="", flush=True)
        finally:
            if killed:
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
            else:
                proc.wait()

    return proc.returncode


def _run_summary(ci_log_summary: Path, log_path: Path, summary_path: Path,
                 step_id: int, round_number: int) -> dict:
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


# Model missing/download failures (offline cache miss, network errors) are
# environment issues, not adaptation bugs — they must not block the step
# (runs 31620090267/31661253547: hunyuan-vl missing from the runner cache
# dragged step-1 into repeated e2e failure chains; the adapter correctly
# found no source change).
_MODEL_DOWNLOAD_FAILURE_RE = re.compile(
    r"Cannot find the requested files in the cached path"
    r"|local_files_only"
    r"|failed to download|download failed|Download failed"
    r"|snapshot_download.*(?:error|failed|Cannot find|connect)"
    r"|offline mode.*(?:model|download)"
    r"|outgoing traffic has been disabled"
    r"|connection (?:error|failed).*(?:modelscope|huggingface)"
    r"|(?:modelscope|huggingface).*connection (?:error|failed)",
    re.IGNORECASE)

# A real (non-download) exception after a traceback means the log carries a
# genuine code/compile bug NEXT TO the download noise — run 31952700363:
# an offline safetensors warning (Qwen3-8B-speculator.eagle3) appeared in the
# same log as the npu_fx_compiler "too many values to unpack (expected 20)"
# crash, and the download signature alone misclassified the whole test as an
# env flake, hiding the real bug from the adapter.
_REAL_ERROR_SIGNATURES = re.compile(
    r"Traceback \(most recent call last\).*?(?:ValueError|RuntimeError|"
    r"TypeError|KeyError|AttributeError|AssertionError|ImportError|"
    r"IndexError|OverflowError|NameError|NotImplementedError|EngineDeadError)",
    re.DOTALL | re.IGNORECASE,
)


def _is_model_download_failure(log_path: Path) -> bool:
    """True if the test log shows a model-missing/download failure and no
    real code error alongside it."""
    try:
        if not log_path.exists() or log_path.stat().st_size > 50 * 1024 * 1024:
            return False
        text = log_path.read_text(encoding="utf-8", errors="replace")
        if not _MODEL_DOWNLOAD_FAILURE_RE.search(text):
            return False
        if _REAL_ERROR_SIGNATURES.search(text):
            ts_print("  [env-flake] real traceback exception found next to "
                     "download noise — NOT classifying as env flake")
            return False
        return True
    except OSError:
        return False


# NPU OOM: when the engine dies of an NPU OOM, vllm's multiproc executor
# hangs instead of exiting (the workers never answer the shutdown), so pytest
# sits until the suite timeout kills it (exit -9).  Free NPU memory is shared
# state on the resident runner — leftover from an earlier test in the round,
# not the adaptation.  Runs 33314256232/33406387872: gemma4 two_card burned
# three 30-min rounds this way with zero fix signal for the adapter.
_NPU_OOM_RE = re.compile(r"NPU out of memory", re.IGNORECASE)

# The multiproc executor wraps each worker's death reason in this message.
_WORKER_ERROR_REASON_RE = re.compile(r"Worker failed with error '([^']*)'")

# pytest --color=yes wraps "AssertionError" etc. in ANSI codes; stripped
# first or the raised-message scan below misses real errors.
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

# A real error WITH A MESSAGE ("RuntimeError: <text>") next to the OOM means
# the run had a genuine code failure — never env-flake it.  Bare keywords
# ("RuntimeError(") are traceback SOURCE lines, not raised messages, and must
# not match (round-2 gemma4 log carries exactly one such line).
_RAISED_ERROR_MSG_RE = re.compile(
    r"\b(?:ValueError|RuntimeError|TypeError|KeyError|AttributeError|"
    r"AssertionError|ImportError|IndexError|OverflowError|OSError|"
    r"NameError|NotImplementedError|EngineDeadError)\b:\s+\S")


def _is_oom_hang_failure(log_path: Path) -> bool:
    """True if the test log shows an NPU-OOM engine death with no real code
    error alongside it (the suite timed out waiting for the dead engine)."""
    try:
        if not log_path.exists() or log_path.stat().st_size > 50 * 1024 * 1024:
            return False
        text = _ANSI_RE.sub("", log_path.read_text(encoding="utf-8",
                                                   errors="replace"))
        if not _NPU_OOM_RE.search(text):
            return False
        # Strip the worker-error wrapper lines before scanning for real
        # errors: the wrapper itself renders as "RuntimeError: ..." with the
        # OOM message inside.
        rest = re.sub(r"^.*Worker failed with error.*$", "", text,
                      flags=re.MULTILINE)
        if _RAISED_ERROR_MSG_RE.search(rest):
            ts_print("  [env-flake] real error with message found next to "
                     "the OOM — NOT classifying as env flake")
            return False
        reasons = _WORKER_ERROR_REASON_RE.findall(text)
        if reasons and not all("out of memory" in r.lower() for r in reasons):
            ts_print("  [env-flake] engine died of a non-OOM worker error "
                     "— NOT classifying as env flake")
            return False
        return True
    except OSError:
        return False


# Numeric-precision assertion failures: the test compares vllm output to a
# reference with torch.allclose / assert_close (or numpy equivalents) and
# the values differ slightly under float16 — run 33721193925's 310p
# classification head (hf [0.0018, 0.9980] vs vllm [0.0018, 0.9982]).
# These get precision_pass (see PASS_RESULTS): tracked, not blocking.
_PRECISION_FAILURE_RE = re.compile(
    r"torch\.allclose|torch\.testing\.assert_close|allclose\("
    r"|numpy\.allclose|np\.allclose"
    r"|assert_allclose|values are not close|maximum absolute difference"
    r"|are not equal to desired equal"
    r"|Mismatched elements:|not equal", re.IGNORECASE)


def _is_precision_failure(log_path: Path) -> bool:
    try:
        if not log_path.exists() or log_path.stat().st_size > 50 * 1024 * 1024:
            return False
        text = _ANSI_RE.sub("", log_path.read_text(encoding="utf-8",
                                                   errors="replace"))
    except OSError:
        return False
    return bool(_PRECISION_FAILURE_RE.search(text))


_ERROR_SIGNATURES = re.compile(
    r"Traceback \(most recent call last\)|Traceback:"
    r"|EngineCore failed|exited unexpectedly"
    r"|\[UC\]\[E\]|\[ERROR\]|\[TIMEOUT\]"
    r"|\b(?:ValueError|RuntimeError|TypeError|KeyError|AttributeError|"
    r"AssertionError|ImportError|ValidationError|IndexError|"
    r"OverflowError|OSError)\b"
)


def _extract_error_excerpt(log_text: str, max_chars: int = 4000) -> str | None:
    """Extract windows around error signatures from a test log.

    A test that hangs after a crash (e.g. a server died but the test keeps
    polling for readiness) buries the real traceback under minutes of log
    noise, so cutting the tail loses the cause.  Search the whole log for
    error signatures and return the surrounding context instead.
    """
    matches = list(_ERROR_SIGNATURES.finditer(log_text))
    if not matches:
        return None
    parts: list[str] = []
    total = 0
    prev_end = -1
    for m in matches:
        start = max(0, m.start() - 200)
        end = min(len(log_text), m.end() + 800)
        if start <= prev_end:
            continue
        piece = log_text[start:end]
        if total + len(piece) > max_chars:
            parts.append(f"... (truncated, {len(matches) - len(parts)} more matches)")
            break
        line_no = log_text.count("\n", 0, m.start()) + 1
        parts.append(f"...[log line {line_no}]\n{piece}")
        total += len(piece)
        prev_end = end
    return "\n\n---\n\n".join(parts) if parts else None


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
            return ["ssh", *_SSH_OPTS, remote_host,
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
        return ["ssh", *_SSH_OPTS, remote_host, inner]
    return [sys.executable, "-m", "pytest", "-sv", "--color=yes", test]


def _run_one_test(cmd: list[str], log_path: Path, summary_path: Path,
                  test: str, devices: str, ci_log_summary: Path,
                  ascend_path: Path, step_id: int, round_number: int,
                  env: dict[str, str], *, is_remote: bool, is_mock: bool,
                  timeout_s: int | None = None) -> dict:
    """Execute one test and return its result dict."""
    cwd = Path("/tmp") if is_remote else ascend_path
    if not is_remote and not is_mock:
        env["ASCEND_RT_VISIBLE_DEVICES"] = devices
    exit_code = _run_to_log(cmd, cwd, log_path, env, timeout_s=timeout_s)
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
    ci_result = _classify(exit_code, s, se)
    if ci_result == "failed" and _is_model_download_failure(log_path):
        ts_print(f"  [env-flake] {test}: model download/cache failure "
                 f"classified as environment, not blocking")
        ci_result = "env_flake_pass"
    if (ci_result != "passed" and exit_code == -9
            and _is_oom_hang_failure(log_path)):
        ts_print(f"  [env-flake] {test}: engine died of NPU OOM and hung to "
                 f"the suite timeout — classified as environment, not "
                 f"blocking")
        ci_result = "env_flake_pass"
    if ci_result == "failed" and _is_precision_failure(log_path):
        ts_print(f"  [precision-pass] {test}: numeric-precision assertion "
                 f"(allclose vs reference) — classified as precision_pass, "
                 f"not blocking (user decision 2026-09-03)")
        ci_result = "precision_pass"
    return {"test": test, "cards_required": cards,
            "run_suite_exit_code": exit_code,
            "ci_result": ci_result,
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
    step_id: int = 0,
    select_by_files: list[str] | None = None,
    test_cases: list[str] | None = None,
    test_timeouts: dict[str, int] | None = None,
    remote: str | None = None,
    log_dir: str | Path = "",
    remote_log_dir: str | Path | None = None,
    remote_vllm_path: str | Path = "/vllm-workspace/vllm",
    remote_ascend_path: str | Path = "/vllm-workspace/vllm-ascend",
    round_number: int = 1,
    dry_run: bool = False,
    sequential: bool = False,
    mock: bool = False,
    mock_scale: float = 0.1,
    skip_setup: bool = False,
    card_overrides: dict[str, int] | None = None,
    pair_aligned_devices: bool = False,
) -> dict:
    """Run end-to-end tests for a main2main step.

    Args:
        select_by_files: Changed file paths for precise test selection
                         via vllm-ascend's select_tests.py.
        skip_setup: Skip the local repo checkout/reinstall (the external
                    E2E exec workflow already checked out the signal
                    branch and installed editable packages — re-running
                    setup_env would reset the tree and reinstall).
        card_overrides: Per-test card counts (authoritative over the path
                        heuristic — test_config.yaml routing can differ,
                        e.g. 310p_x4 for a one_card path, a3_x2 for NPU
                        UT paths).
    """
    vllm_path = Path(vllm_path)
    ascend_path = Path(ascend_path)
    if patch_path:
        patch_path = Path(patch_path)
    log_dir = Path(log_dir)
    remote_log_dir = Path(remote_log_dir) if remote_log_dir else log_dir
    remote_vllm = Path(remote_vllm_path) if remote_vllm_path else Path("/vllm-workspace/vllm")
    remote_ascend = Path(remote_ascend_path) if remote_ascend_path else Path("/vllm-workspace/vllm-ascend")

    _CARD_OVERRIDES.clear()
    if card_overrides:
        _CARD_OVERRIDES.update(card_overrides)

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
        ts_print("No tests to run.", flush=True)
        return {"can_commit": True, "ci_result": "passed", "suite_results": {}}

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
        ts_print("Error: could not detect any NPU cards", file=sys.stderr)
        sys.exit(1)
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
        proc = subprocess.Popen(["ssh", *_SSH_OPTS, remote_host, inner],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        assert proc.stdout is not None
        for line in proc.stdout:
            ts_print(line, end="", flush=True)
        if proc.wait() != 0:
            raise RuntimeError(f"Remote setup failed with exit code {proc.returncode}")
    elif not skip_setup:
        setup_env(vllm_path, vllm_commit, ascend_path, ascend_commit, patch_path)
    else:
        ts_print(f"  skip_setup: using checked-out repos as-is "
                 f"(ascend @ {ascend_commit[:8]})")

    # ---- step 5: locate ci_log_summary ----
    ci_log_summary = Path(__file__).parent / "ci_log_summary.py"

    # ---- step 6: env ----
    env = os.environ.copy()
    env.setdefault("VLLM_WORKER_MULTIPROC_METHOD", "spawn")
    env.setdefault("VLLM_USE_MODELSCOPE", "true")

    # ---- step 7: schedule ----
    ci_dir = log_dir / str(step_id) / "tests"
    result_path = ci_dir / f"round-{round_number}-result.json"
    # Load estimated test times for optimal ordering (longest first → shorter makespan)
    est_times = _load_estimated_times(ascend_path) if not remote_host else {}
    if est_times:
        total_est = sum(_lookup_time(t, est_times) for t in test_files)
        ts_print(f"Estimated test durations loaded ({len(est_times)} entries, "
              f"total: {total_est // 60} min for {len(test_files)} tests)")
    overriders = _detect_device_overriders(test_files, ascend_path)
    if overriders:
        ts_print(f"Device-overriding tests (own physical 0..N-1, one per round): "
                 f"{sorted(overriders)}", flush=True)
    rounds = [[t] for t in test_files] if sequential else _schedule_rounds(
        test_files, total_cards, est_times, device_overriders=overriders)
    if pair_aligned_devices:
        ts_print(f"  Dual-die pairing: enforcing pair-aligned device assignment "
                 f"(devices {phy_ids})", flush=True)
    device_rounds = _assign_devices(rounds, all_phy_ids,
                                    pair_aligned=pair_aligned_devices)

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
        return {}

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
                                      is_remote=bool(remote_host), is_mock=mock,
                                      timeout_s=test_timeouts.get(test) if test_timeouts else None)
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

        if remote_host:
            remote_ci = f"{remote_log_dir}/{step_id}/tests"
            ts_print(f"  Pulling remote logs: {remote_host}:{remote_ci} -> {ci_dir}", flush=True)
            _sync_remote_dir(remote_host, remote_ci, ci_dir)

    total_elapsed = time.monotonic() - t0
    if remote_host:
        ts_print(f"\n=== Final log sync ===", flush=True)
        _sync_remote_dir(remote_host, f"{remote_log_dir}/{step_id}/tests", ci_dir)

    # ---- step 9: aggregate ----
    result = aggregate_suite_results(
        step_id=step_id, round_number=round_number, all_results=all_results,
        total_cards=total_cards, sequential=sequential, remote=remote,
        ci_dir=ci_dir, rounds_info=rounds_info, total_elapsed=total_elapsed,
    )
    result_path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    ts_print(f"\nmain2main CI aggregated: {result['ci_result']}  "
             f"(can_commit={result['can_commit']})", flush=True)
    ts_print(f"Total elapsed: {total_elapsed:.1f}s  ({total_elapsed/60:.1f} min)", flush=True)
    ts_print(f"Result written to {result_path}", flush=True)
    return result


def aggregate_suite_results(
    step_id: int,
    round_number: int,
    all_results: list[dict],
    total_cards: int,
    sequential: bool,
    remote: str | None,
    ci_dir: Path,
    rounds_info: list[dict],
    total_elapsed: float,
) -> dict:
    """Aggregate per-test results into the run_tests() result dict (step 9).

    Shared with the external E2E dispatcher (e2e_dispatch.py) so both
    paths produce byte-identical result JSON for fix mode.
    """
    outcomes = {r["ci_result"] for r in all_results}
    if "failed" in outcomes:
        overall = "failed"
    elif "summary_error" in outcomes:
        overall = "summary_error"
    elif outcomes == {"passed"}:
        overall = "passed"
    elif "env_flake_pass" in outcomes:
        overall = "env_flake_pass"
    else:
        # precision_pass only (with passed) — pass, tracked separately.
        overall = "precision_pass"

    return {
        "step_id": step_id, "round": round_number,
        "label": "+".join(r["test"] for r in all_results),
        "tests": [r["test"] for r in all_results],
        "ci_result": overall, "passed": overall == "passed",
        "can_commit": overall in PASS_RESULTS, "requires_fix": overall == "failed",
        "log_path": str(ci_dir), "summary_path": str(ci_dir),
        "total_cards": total_cards, "sequential": sequential, "remote": remote,
        "elapsed_s": round(total_elapsed, 1), "rounds": rounds_info,
        "suite_results": {r["test"]: r for r in all_results},
        "code_bugs_count": sum(r["code_bugs_count"] for r in all_results),
        "env_flakes_count": sum(r["env_flakes_count"] for r in all_results),
        "failed_test_files_count": sum(r["failed_test_files_count"] for r in all_results),
        "failed_test_cases_count": sum(r["failed_test_cases_count"] for r in all_results),
    }


def build_test_errors_detail(
    suite_results: dict,
    round_number: int,
    tests_dir: Path,
    result_json: Path,
) -> Path | None:
    """Write the fix-mode test-errors.txt from per-test suite results.

    For each non-passed test, inline the log excerpt (error signatures
    windowed from the WHOLE log — a hang after a crash buries the real
    traceback under minutes of READY/polling noise, so cutting the tail
    loses the cause), the log tail as fallback, and the structured
    ci_log_summary output.  env_flake_pass tests are ALSO included: their
    logs often carry the real root cause that the classifier smoothed over
    (e.g. torch_npu npugraph FX compile crash "too many values to unpack
    (expected 20)" classified as Engine-core-init failure — run
    31952700363).  Without the excerpt the adapter cannot tell a true env
    issue from a real bug and burns fix rounds guessing.

    Excerpt FIRST (log content before the structured summary): a
    timeout-killed test has an empty summary (no pytest output), and an
    empty [summary] misled the adapter into dismissing real bugs as
    "resource kill" (run 31376860112: disaggregated_encoder's eps=0.0
    ValueError sat in the excerpt, but the empty summary above it won).

    Returns the detail file path, or None when every test passed (caller
    falls back to the result JSON alone).
    """
    detail_parts: list[str] = []
    for test_name, tr in suite_results.items():
        if tr.get("ci_result") == "passed":
            continue
        parts = [f"=== {test_name} ==="]
        if tr.get("not_run"):
            # External E2E (e2e_dispatch.py): the exec job/group crashed
            # before this test — there is no log to excerpt.
            parts.append("[NOTE] test was NOT run by the E2E job — the "
                         "job/group failed before reaching it (no log "
                         "available)")
            detail_parts.append("\n\n".join(parts))
            continue
        lp = Path(tr.get("log_path", ""))
        log_text = ""
        if lp.exists():
            try:
                log_text = lp.read_text(encoding="utf-8", errors="replace")
            except Exception:
                parts.append("[log]\n(could not read)")
        if log_text:
            excerpt = _extract_error_excerpt(log_text)
            if excerpt:
                parts.append(f"[log excerpt]\n{excerpt}")
            else:
                parts.append(f"[log tail]\n...\n{log_text[-3000:]}")
        if not log_text and tr.get("run_suite_exit_code") == -9:
            parts.append("[NOTE] process was killed by the suite timeout "
                         "(exit -9) and its log is empty — likely a hang "
                         "with no error output")
        sp = Path(tr.get("summary_path", ""))
        if sp.exists():
            try:
                summary_text = sp.read_text(encoding="utf-8")[:4000]
            except Exception:
                summary_text = "(could not read)"
            if '"code_bugs": []' in summary_text.replace(" ", ""):
                summary_text += ("\n[NOTE] structured summary is empty — the test was "
                                 "killed by the suite timeout (no pytest output). "
                                 "The [log excerpt] above IS the failure; do not "
                                 "dismiss it as a resource kill.")
            parts.append(f"[summary]\n{summary_text}")
        detail_parts.append("\n\n".join(parts))
    if not detail_parts:
        return None
    test_errors_detail = tests_dir / f"round-{round_number}-test-errors.txt"
    test_errors_detail.write_text("\n\n---\n\n".join(detail_parts), encoding="utf-8")
    return test_errors_detail


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
    p.add_argument("--step-id", type=int, required=True)
    p.add_argument("--round", type=int, default=1)
    p.add_argument("--select-by-files", nargs="*", default=None,
                   help="Changed file paths for precise test selection via select_tests.py.")
    p.add_argument("--test-cases", nargs="*", default=None,
                   help="Explicit test targets; append '@N' to pin N cards "
                        "(e.g. tests/.../one_card/test_x.py@4), overriding "
                        "the path heuristic with test_config routing.")
    p.add_argument("--skip-setup", action="store_true",
                   help="Do not checkout/reinstall repos (external E2E: the "
                        "signal branch + editable install are already in place).")
    p.add_argument("--log-dir", type=Path, default=Path("."))
    p.add_argument("--sequential", action="store_true")
    p.add_argument("--remote")
    p.add_argument("--remote-vllm-path", type=Path)
    p.add_argument("--remote-ascend-path", type=Path)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--pair-aligned-devices", action="store_true",
                   help="Dual-die NPU (A3): only ever assign whole die pairs "
                        "(0-1, 2-3, ...) to tests; never start a test on an "
                        "odd die.  Validates the runner's visible devices are "
                        "closed under pairing before scheduling.")
    p.add_argument("--mock", action="store_true")
    p.add_argument("--mock-scale", type=float, default=0.1)
    args = p.parse_args()

    test_cases: list[str] | None = None
    card_overrides: dict[str, int] = {}
    if args.test_cases:
        test_cases = []
        for token in args.test_cases:
            test, sep, cards = token.rpartition("@")
            if sep and cards.isdigit():
                test_cases.append(test)
                card_overrides[test] = int(cards)
            else:
                test_cases.append(token)

    result = run_tests(
        vllm_path=args.vllm_path, vllm_commit=args.vllm_commit,
        ascend_path=args.ascend_path, ascend_commit=args.ascend_commit,
        patch_path=args.patch, step_id=args.step_id,
        select_by_files=args.select_by_files,
        test_cases=test_cases, card_overrides=card_overrides,
        remote=args.remote, log_dir=args.log_dir,
        remote_vllm_path=args.remote_vllm_path,
        remote_ascend_path=args.remote_ascend_path,
        round_number=args.round, dry_run=args.dry_run,
        sequential=args.sequential, mock=args.mock, mock_scale=args.mock_scale,
        skip_setup=args.skip_setup,
        pair_aligned_devices=args.pair_aligned_devices,
    )
    sys.exit(0 if result.get("can_commit", False) else 1)


if __name__ == "__main__":
    main()
