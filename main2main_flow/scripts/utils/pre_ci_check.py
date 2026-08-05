"""Pre-CI verification for main2main steps.

Runs two mechanical checks before CI to catch common adaptation errors:
  1. Version string consistency: newly added vllm_version_is() calls use
     the correct release tag (scoped to current diff, not the whole repo).
  2. Temp file cleanliness: no intermediate files in the repository.

Design note:
    The version string check only examines lines ADDED in the current diff
    (git diff HEAD), not the entire repo. Previous main2main runs leave
    behind guards like vllm_version_is("0.20.2") that are correct for that
    version boundary. Scanning the full repo would flag all historical guards
    as mismatches whenever the release tag advances.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from main2main_flow.scripts.utils.utils import run_format_sh, run_git, ts_print

_TEMP_PATTERNS = [
    ".log",
    ".patch",
    ".jsonl",
    "vllm_changes.md",
    "vllm_error_analyze.md",
    "round-ledger",
    "main2main-failure-summary",
    "ci-summary",
]

_VERSION_IS_RE = re.compile(r'vllm_version_is\(\s*["\']([^"\']+)["\']\s*\)')


def _get_added_lines(repo: Path, base_ref: str | None = None) -> list[dict[str, str]]:
    """Get lines added in the working tree vs *base_ref*.

    Defaults to ``upstream/main`` so that incremental mode (rebase from
    baseline) catches all accumulated diffs, not just the current step's.
    Falls back to ``HEAD`` if upstream/main is unavailable.
    """
    if base_ref is None:
        try:
            run_git(repo, "merge-base", "HEAD", "upstream/main")
            base_ref = "upstream/main"
        except subprocess.CalledProcessError:
            base_ref = "HEAD"
    diff_output = run_git(repo, "diff", base_ref, "-U0")
    added: list[dict[str, str]] = []
    current_file = None
    current_line = 0

    for line in diff_output.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
        elif line.startswith("@@ "):
            match = re.search(r'\+(\d+)', line)
            if match:
                current_line = int(match.group(1))
        elif line.startswith("+") and not line.startswith("+++"):
            if current_file:
                added.append({
                    "file": current_file,
                    "line_no": str(current_line),
                    "text": line[1:],
                })
            current_line += 1
        elif not line.startswith("-"):
            current_line += 1

    return added


def _check_version_strings(added_lines: list[dict[str, str]], release_tag: str) -> dict:
    new_calls: list[dict[str, str]] = []
    mismatched: list[dict[str, str]] = []

    for entry in added_lines:
        text = entry["text"]
        if text.lstrip().startswith("#"):
            continue
        if "import " in text or "def " in text:
            continue
        match = _VERSION_IS_RE.search(text)
        if not match:
            continue
        version_used = match.group(1)
        call_info = {
            "file": entry["file"],
            "line": entry["line_no"],
            "version_used": version_used,
            "text": text.strip(),
        }
        new_calls.append(call_info)
        if version_used != release_tag:
            mismatched.append(call_info)

    return {
        "release_tag": release_tag,
        "new_calls_count": len(new_calls),
        "mismatched": mismatched,
    }


def _check_temp_files(repo: Path) -> dict:
    status_output = run_git(repo, "status", "--short")
    untracked_output = run_git(repo, "ls-files", "--others", "--exclude-standard")

    all_files: set[str] = set()
    for line in (status_output + untracked_output).strip().splitlines():
        filepath = line.strip().lstrip("MADRCU?! ").strip()
        if filepath:
            all_files.add(filepath)

    violations: list[str] = []
    for filepath in sorted(all_files):
        basename = Path(filepath).name
        for pattern in _TEMP_PATTERNS:
            if pattern in basename or basename.endswith(pattern):
                violations.append(filepath)
                break

    return {"violations": violations}


def _check_format(repo: Path) -> dict:
    """Run ``bash format.sh`` and detect real (non-auto-fixable) errors.

    Auto-fix hooks (ruff-format, ruff-check --fix) report "Failed" when they
    modify files — that's expected, not an error.  Environment-level failures
    (shellcheck not installed, Exec format error) are also ignored.

    Real errors come from hooks that CANNOT auto-fix: ruff E501/F821,
    codespell typos, typos, etc.  These are detected by checking each
    failed hook's output for actual violation lines.
    """
    fmt_script = repo / "format.sh"
    if not fmt_script.exists():
        ts_print("\n[pre_ci] format: SKIPPED — format.sh not found")
        return {"violations": [], "detail": "format.sh not found", "skipped": True}
    if not shutil.which("pre-commit"):
        ts_print("\n[pre_ci] format: SKIPPED — pre-commit not installed, all lint checks bypassed!")
        return {"violations": [], "detail": "pre-commit not installed", "skipped": True}

    ts_print("\n[pre_ci] === format.sh output begin ===")
    r = run_format_sh(repo)
    output = (r.stdout + "\n" + r.stderr)
    ts_print(output.strip())
    ts_print(f"[pre_ci] === format.sh output end (exit={r.returncode}) ===")

    diff_after = subprocess.run(
        ["git", "diff", "--stat"], cwd=str(repo), capture_output=True, text=True,
    ).stdout.strip()
    if diff_after:
        ts_print(f"[pre_ci] format.sh modified files in working tree:\n{diff_after}")

    # Extract real errors — hook-level, not regex-based line parsing.
    # For each FAILED hook, skip auto-fix noise and env-related failures;
    # everything else is a real violation.
    real_errors: list[str] = []
    for hook_name, hook_lines in _iter_failed_hooks(output):
        real_lines = [l for l in hook_lines if _is_real_error(l)]
        if real_lines:
            real_errors.extend(real_lines)

    if real_errors:
        ts_print(f"\n[pre_ci] format: {len(real_errors)} non-auto-fixable issue(s):")
        for e in real_errors[:20]:
            ts_print(f"  {e}")
        return {"violations": real_errors,
                "detail": f"{len(real_errors)} lint issue(s) (not auto-fixable)"}
    ts_print("\n[pre_ci] format: OK")
    return {"violations": [], "detail": "format.sh OK"}


def _iter_failed_hooks(output: str):
    """Yield (hook_name, lines) for each failed hook in pre-commit output."""
    lines = output.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # Hook status line: "ruff check.....................................................Failed"
        if line.endswith("Failed") and "..." in line:
            hook_name = line.rstrip(".").rstrip()
            hook_lines: list[str] = []
            i += 1
            # Collect lines until next hook or end
            while i < len(lines):
                nl = lines[i].strip()
                # Next hook status line (either Passed or Failed)
                if nl.endswith("Passed") and "..." in nl:
                    break
                if nl.endswith("Failed") and "..." in nl:
                    break
                hook_lines.append(lines[i])
                i += 1
            yield hook_name, hook_lines
        else:
            i += 1


def _is_real_error(line: str) -> bool:
    """Check if a hook output line represents a real (non-auto-fixable) error."""
    s = line.strip()
    if not s:
        return False
    # Auto-fix noise
    if "files were modified" in s or "file reformatted" in s or "files reformatted" in s:
        return False
    if "files left unchanged" in s:
        return False
    # pre-commit metadata
    if s.startswith("- hook id:") or s.startswith("- exit code:") or s.startswith("- duration:"):
        return False
    # Environment issues
    if "Please install shellcheck" in s or "Exec format error" in s:
        return False
    if "To bypass pre-commit hooks" in s:
        return False
    # gitleaks / shell permission issues are infrastructure, not adaptation
    if "is not executable" in s:
        return False
    if "gitleaks" in s.lower():
        return False
    # Only report lines that look like actual lint violations:
    # file.EXT:LINE:COL: CODE or file.EXT:LINE: CODE
    if not re.match(r'^[\w/.-]+\.\w+:\d+:', s):
        return False
    return True


def _check_mypy(repo: Path, vllm_path: str | Path | None = None) -> dict:
    """Run mypy with the same core command and environment as vllm-ascend's CI.

    Mirrors the CI pre-commit job's "Run mypy" step: for each python version
    in ``3.10/3.11/3.12`` (the matrix CI runs), runs::

        PYTHONPATH=<vllm_path> mypy --follow-imports skip --check-untyped-defs \\
            --python-version <X.Y> --exclude _cann_ops_custom/ \\
            vllm_ascend examples tests

    ``PYTHONPATH=<vllm_path>`` points mypy at vllm source (CI uses
    ``./vllm-empty`` at verified commit). ``--exclude _cann_ops_custom/``
    handles the build artifact dir that CI's lint image doesn't have.

    Returns all errors mypy reports across all python versions - no
    added-line filtering.  CI mypy is the source of truth; if it fails,
    the adaptation must be fixed.
    """
    mypy = shutil.which("mypy")
    if not mypy:
        return {"violations": [], "detail": "mypy not installed", "skipped": True}

    import os as _os
    env = _os.environ.copy()
    if vllm_path:
        vllm_abs = str(Path(vllm_path).resolve())
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{vllm_abs}:{existing}" if existing else vllm_abs
        ts_print(f"\n[pre_ci] mypy: PYTHONPATH includes vllm source: {vllm_abs}")

    # CI's lint image runs mypy in a clean environment: no vllm package
    # installed (mypy uses PYTHONPATH for vllm source), numpy 1.26.4
    # (constrained by triton-ascend's metadata, installed WITHOUT --no-deps).
    #
    # main2main runner has vllm installed (target commit, for e2e) and numpy
    # 2.x (workflow installs triton-ascend with --no-deps, skipping the numpy
    # constraint).  This causes ~68 spurious mypy errors.
    #
    # Fix: create an isolated venv with --system-site-packages (inherits mypy,
    # mypy.ini, triton-ascend, etc. from the system), install only the numpy
    # version that triton-ascend constrains (dynamically read from metadata,
    # not hardcoded), and DON'T install vllm (so mypy falls back to PYTHONPATH).
    # This exactly reproduces the CI lint image's mypy environment.
    # The venv is temporary and destroyed after the mypy run.
    #
    # Verified: venv + numpy==1.26.4 + no vllm + PYTHONPATH=vllm source
    #   -> 0 errors (matches clean lint image exactly).
    import tempfile
    import importlib.metadata as _md

    # Read numpy constraint from triton-ascend metadata (not hardcoded).
    # Parse with packaging.requirements.Requirement so COMPOUND specifiers
    # (e.g. "numpy>=1.26.4,<2.1") are handled - the old regex only captured
    # the first comparator, turning ">=1.26.4,<2.1" into ">=1.26.4" which
    # resolves to numpy 2.x and reproduces the false positives.
    target_numpy_spec = ""
    try:
        from packaging.requirements import Requirement
        reqs = _md.requires("triton-ascend") or []
        for req in reqs:
            if "extra" in req.lower():
                continue
            try:
                r = Requirement(req)
            except Exception:
                continue
            if r.name.lower() == "numpy":
                # Reconstruct full spec: "numpy>=1.26.4,<2.1" -> ">=1.26.4,<2.1"
                target_numpy_spec = ",".join(
                    f"{s.operator}{s.version}" for s in r.specifier)
                break
    except Exception as e:
        ts_print(f"[pre_ci] mypy: failed to read triton-ascend numpy constraint ({e})")

    if not target_numpy_spec:
        ts_print("[pre_ci] mypy: WARNING no triton-ascend numpy constraint found - "
                 "using system numpy (may report spurious [var-annotated] errors, "
                 "see known numpy 2.x issue)")

    # Create isolated venv to run mypy in CI-lint-equivalent environment.
    venv_dir = None
    mypy_cmd = [mypy]  # default: use system mypy
    if vllm_path and target_numpy_spec:
        venv_dir = Path(tempfile.mkdtemp(prefix="mypy_lint_venv_"))
        ts_print(f"[pre_ci] mypy: creating lint-equivalent venv at {venv_dir} "
                 f"(numpy{target_numpy_spec} from triton-ascend, no vllm package)")
        try:
            r = subprocess.run(
                [sys.executable, "-m", "venv", str(venv_dir), "--system-site-packages"],
                capture_output=True, text=True, timeout=180,
            )
        except subprocess.TimeoutExpired:
            ts_print("[pre_ci] mypy: WARNING venv creation TIMED OUT (180s) - "
                     "falling back to system mypy (may report spurious numpy 2.x errors)")
            # keep venv_dir set so finally cleans up the partial venv dir
            r = None
        if r and r.returncode == 0:
            venv_python = venv_dir / "bin" / "python"
            # Install numpy constraint in venv (overrides system numpy 2.x).
            # Check returncode - a failed install silently leaves system
            # numpy 2.x in the venv, reproducing the false positives.
            try:
                r2 = subprocess.run(
                    [str(venv_python), "-m", "pip", "install", f"numpy{target_numpy_spec}",
                     "--no-build-isolation"],
                    capture_output=True, text=True, timeout=180,
                )
            except subprocess.TimeoutExpired:
                ts_print("[pre_ci] mypy: WARNING numpy install in venv TIMED OUT (180s) - "
                         "falling back to system mypy")
                r2 = None
            if r2 is not None and r2.returncode != 0:
                ts_print(f"[pre_ci] mypy: numpy install in venv FAILED "
                         f"({r2.stderr.strip()[:300]}) - falling back to system mypy")
                # keep venv_dir set so finally cleans up the venv dir
            elif r2 is not None:
                # Verify the actual installed numpy version satisfies the spec.
                try:
                    vr = subprocess.run(
                        [str(venv_python), "-c", "import numpy; print(numpy.__version__)"],
                        capture_output=True, text=True, timeout=30,
                    )
                except subprocess.TimeoutExpired:
                    vr = None
                installed = vr.stdout.strip() if (vr and vr.returncode == 0) else "?"
                ts_print(f"[pre_ci] mypy: venv numpy installed: {installed} "
                         f"(expected spec numpy{target_numpy_spec})")
                # Check spec satisfaction - a compound spec like ">=1.26.4,<2.1"
                # may resolve to numpy 2.0.x which STILL triggers the spurious
                # [var-annotated] errors (2.x has stricter type stubs).  Warn on
                # any numpy 2.x regardless of spec satisfaction - the known-good
                # state verified in the lint image was numpy 1.26.4.
                if installed.startswith("2."):
                    ts_print(f"[pre_ci] mypy: WARNING installed numpy {installed} is 2.x - "
                             f"results may contain spurious [var-annotated] errors "
                             f"(lint image uses numpy 1.26.4)")
                else:
                    try:
                        from packaging.specifiers import SpecifierSet
                        if not SpecifierSet(target_numpy_spec).contains(installed):
                            ts_print(f"[pre_ci] mypy: WARNING installed numpy {installed} "
                                     f"does NOT satisfy numpy{target_numpy_spec} - "
                                     f"results may contain spurious [var-annotated] errors")
                    except Exception:
                        pass
                # venv inherits system-site-packages (mypy, mypy.ini, triton-ascend)
                # but vllm is NOT installed in venv (system vllm is shadowed by
                # venv's own site-packages which doesn't have it).
                # Use `python -m mypy` (mypy's console script may not exist in
                # venv/bin since it's inherited from system, not installed in venv).
                mypy_cmd = [str(venv_python), "-m", "mypy"]
                ts_print(f"[pre_ci] mypy: using venv mypy via {venv_python} -m mypy")
        elif r is not None:
            ts_print(f"[pre_ci] mypy: venv creation failed ({r.stderr.strip()[:200]}), "
                     f"using system mypy")
            # keep venv_dir set so finally cleans up the partial venv dir

    # Clear mypy cache - it may have cached type info from numpy 2.x or
    # the installed vllm package (target commit).
    import shutil as _shutil
    cache = repo / ".mypy_cache"
    if cache.exists():
        _shutil.rmtree(cache, ignore_errors=True)
        ts_print("\n[pre_ci] mypy: cleared .mypy_cache")

    try:
        all_violations: list[str] = []
        all_output: list[str] = []
        any_failed = False
        for py_ver in ("3.10", "3.11", "3.12"):
            ts_print(f"[pre_ci] === mypy --python-version {py_ver} output begin ===")
            r = subprocess.run(
                [*mypy_cmd, "--follow-imports", "skip", "--check-untyped-defs",
                 "--python-version", py_ver,
                 "--exclude", "_cann_ops_custom/",
                 "vllm_ascend"],
                cwd=str(repo), capture_output=True, text=True, env=env,
            )
            output = r.stdout + "\n" + r.stderr
            ts_print(output.strip())
            ts_print(f"[pre_ci] === mypy output end (py={py_ver}, exit={r.returncode}) ===")
            all_output.append(f"--- python {py_ver} (exit={r.returncode}) ---\n{output}")
            if r.returncode != 0:
                any_failed = True
    finally:
        # Destroy the temporary venv (no need to restore anything - the
        # main environment was never touched).
        if venv_dir and venv_dir.exists():
            _shutil.rmtree(venv_dir, ignore_errors=True)
            ts_print(f"[pre_ci] mypy: destroyed temporary venv at {venv_dir}")

    if not any_failed:
        ts_print("\n[pre_ci] mypy: OK (all 3 python versions clean)")
        return {"violations": [], "detail": "mypy clean (3.10/3.11/3.12)"}

    _MYPY_ERR_RE = re.compile(r"^(.+\.py):(\d+):(?:\d+:)?\s*error:")
    seen: set[str] = set()
    for line in "\n".join(all_output).splitlines():
        stripped = line.strip()
        if _MYPY_ERR_RE.search(stripped) and stripped not in seen:
            seen.add(stripped)
            all_violations.append(stripped)

    if all_violations:
        ts_print(f"\n[pre_ci] mypy: {len(all_violations)} unique issue(s):")
        for v in all_violations[:20]:
            ts_print(f"  {v}")
        if len(all_violations) > 20:
            ts_print(f"  ... and {len(all_violations) - 20} more (see pre_ci_check.json)")
        return {"violations": all_violations,
                "detail": f"{len(all_violations)} mypy issue(s) (3.10/3.11/3.12)"}
    ts_print("\n[pre_ci] mypy: FAILED but no parseable error lines")
    return {"violations": ["\n".join(all_output)[-2000:]],
            "detail": "mypy failed but no parseable errors"}


# CPU UT routing: mirror select_tests._scan_ut_test_dir(cpu_only=True).
# Files under tests/ut/<module>/a2/ or tests/ut/<module>/a3_2/ route to NPU
# runners (per runner_mapping in test_config.yaml) and are skipped here.
_CPU_UT_A2_RE = re.compile(r"tests/ut/.+/a2(/|$)")
_CPU_UT_A3_2_RE = re.compile(r"tests/ut/.+/a3_2(/|$)")

# CI's CPU UT batch runs with this env to prevent torch from auto-loading
# device backends.  tests/ut/conftest.py then mocks torch_npu when npu-smi
# is unavailable (the CPU-runner case).
_UT_CPU_ENV = {
    "TORCH_DEVICE_BACKEND_AUTOLOAD": "0",
    "VLLM_WORKER_MULTIPROC_METHOD": "spawn",
}

# CI's NPU UT/E2E batch runs with this env: real NPU device, no autoload
# override (torch_npu loads normally), spawn for multiprocess.
_UT_NPU_ENV = {
    "VLLM_WORKER_MULTIPROC_METHOD": "spawn",
    "VLLM_USE_MODELSCOPE": "True",
    "HF_HUB_OFFLINE": "1",
    "VLLM_LOGGING_LEVEL": "ERROR",
}

# E2E tests that CI runs on A2 NPU and are known to catch adaptation bugs.
# Added to the pre-push gate when NPU is available, so we catch the same
# failures CI would catch (e.g., test_extract_hidden_states caught a
# "No common block size for 16" engine-init bug in PR #13515).
_A2_NPU_E2E_TESTS = [
    "tests/e2e/pull_request/one_card/spec_decode/test_extract_hidden_states.py",
]

# A2 NPU E2E tests take ~7 min each (model load + inference); allow 25 min
# for the E2E batch to account for cache misses and slow NPU cards.
_A2_NPU_E2E_TIMEOUT_S = 1500


def _npu_available() -> bool:
    """Detect whether a real Ascend NPU is available on this runner.

    main2main runs on ``linux-aarch64-a2b1-8`` (A2 8-card NPU) — when NPU
    is available, we can run A2 NPU UT + a subset of E2E tests pre-push.
    When unavailable (CPU-only dev box), we fall back to CPU UT only.
    """
    try:
        r = subprocess.run(
            ["npu-smi", "info"],
            capture_output=True, text=True, timeout=10,
        )
        return r.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _is_npu_convention_ut_path(rel_path: str) -> bool:
    """Mirror select_tests._route_ut_dir — True if path routes to NPU runner."""
    p = rel_path.replace("\\", "/")
    return bool(_CPU_UT_A2_RE.search(p) or _CPU_UT_A3_2_RE.search(p))


def _is_a2_npu_ut_path(rel_path: str) -> bool:
    """True if path routes to A2 NPU runner (tests/ut/*/a2/)."""
    p = rel_path.replace("\\", "/")
    return bool(_CPU_UT_A2_RE.search(p))


def _collect_cpu_ut_files(repo: Path) -> list[str]:
    """Walk tests/ut/ for test_*.py, return CPU-routed paths (rel to repo).

    Mirrors select_tests._scan_ut_test_dir('tests/ut', cpu_only=True):
    skips files under a2/ or a3_2/ subdirs (NPU-convention directories).
    """
    ut_dir = repo / "tests" / "ut"
    if not ut_dir.exists():
        return []
    files: list[str] = []
    for root, dirs, fnames in os.walk(ut_dir):
        if "__pycache__" in dirs:
            dirs.remove("__pycache__")
        for f in sorted(fnames):
            if f.startswith("test_") and f.endswith(".py"):
                rel = os.path.relpath(os.path.join(root, f), str(repo))
                if not _is_npu_convention_ut_path(rel):
                    files.append(rel)
    return files


def _collect_a2_npu_ut_files(repo: Path) -> list[str]:
    """Walk tests/ut/ for A2 NPU-routed test files (tests/ut/*/a2/).

    These run on the A2 NPU runner (main2main is linux-aarch64-a2b1-8).
    Does NOT include a3_2/ (A3 NPU, different arch) — main2main can't run A3.
    """
    ut_dir = repo / "tests" / "ut"
    if not ut_dir.exists():
        return []
    files: list[str] = []
    for root, dirs, fnames in os.walk(ut_dir):
        if "__pycache__" in dirs:
            dirs.remove("__pycache__")
        for f in sorted(fnames):
            if f.startswith("test_") and f.endswith(".py"):
                rel = os.path.relpath(os.path.join(root, f), str(repo))
                if _is_a2_npu_ut_path(rel):
                    files.append(rel)
    return files


def _source_ascend_env(env: dict) -> dict:
    """Source /usr/local/Ascend/ascend-toolkit/set_env.sh and merge into env.

    NPU tests need the Ascend toolkit's LD_LIBRARY_PATH and other env vars
    to be set.  CI does `. /usr/local/Ascend/ascend-toolkit/set_env.sh` before
    running NPU tests; we replicate by sourcing and parsing the env delta.
    """
    set_env_script = "/usr/local/Ascend/ascend-toolkit/set_env.sh"
    if not os.path.exists(set_env_script):
        ts_print(f"[pre_ci] ut: WARNING {set_env_script} not found — "
                 "NPU tests may fail to import torch_npu")
        return env
    # Source the script and dump env as NUL-separated key=value pairs.
    # This captures all env vars the script sets (LD_LIBRARY_PATH, etc.).
    r = subprocess.run(
        ["bash", "-c", f"source {set_env_script} && env -0"],
        capture_output=True, text=True, timeout=10,
    )
    if r.returncode != 0:
        ts_print(f"[pre_ci] ut: WARNING source set_env.sh failed "
                 f"({r.stderr.strip()[:200]})")
        return env
    new_env = dict(env)
    for entry in r.stdout.split("\0"):
        if not entry or "=" not in entry:
            continue
        key, _, value = entry.partition("=")
        new_env[key] = value
    return new_env


def _check_ut(repo: Path, vllm_path: str | Path | None = None,
              timeout_s: int = 1800) -> dict:
    """Run vllm-ascend UT batch (CPU + A2 NPU + E2E smoke) pre-push.

    Coverage (mirrors CI's select_tests routing):
      - CPU UT (158 files in tests/ut/* excluding a2/ a3_2/) — always runs.
        Uses TORCH_DEVICE_BACKEND_AUTOLOAD=0 + conftest.py mocks torch_npu.
      - A2 NPU UT (~15 files in tests/ut/*/a2/) — only when NPU is available
        (main2main runs on linux-aarch64-a2b1-8).  Sources Ascend toolkit env,
        uses real NPU device.
      - A2 NPU E2E smoke (test_extract_hidden_states.py) — only when NPU is
        available.  Catches engine-init bugs that UT can't (e.g., PR #13515's
        "No common block size for 16" failure).  ~7 min per run.

    Venv setup mirrors ``_check_mypy``: ``--system-site-packages`` inherits
    torch/vllm/vllm_ascend/pytest; install ``numpy==1.26.4`` (from triton-ascend
    metadata) to override system numpy 2.x.  Don't install vllm/vllm_ascend
    (editable + PYTHONPATH resolves them to the working tree).

    Returns dict with ``violations`` (list of failing test node IDs or error
    messages) and ``detail``.  Empty violations + non-skipped → pass.
    """
    import tempfile
    import importlib.metadata as _md

    cpu_files = _collect_cpu_ut_files(repo)
    if not cpu_files:
        ts_print("\n[pre_ci] ut: SKIPPED — tests/ut not found or no CPU tests")
        return {"violations": [], "detail": "tests/ut not found", "skipped": True}

    pytest_bin = shutil.which("pytest")
    if not pytest_bin:
        ts_print("\n[pre_ci] ut: SKIPPED — pytest not installed")
        return {"violations": [], "detail": "pytest not installed", "skipped": True}

    ts_print(f"\n[pre_ci] ut: collected {len(cpu_files)} CPU test files "
             f"(skipped NPU-convention a2/ and a3_2/ subdirs)")

    # Detect NPU availability — main2main runs on linux-aarch64-a2b1-8
    # (A2 8-card NPU).  When NPU is available, also run A2 NPU UT + an E2E
    # smoke test to catch bugs CPU UT can't (e.g., engine-init failures).
    npu_ok = _npu_available()
    a2_npu_files: list[str] = []
    e2e_files: list[str] = []
    if npu_ok:
        a2_npu_files = _collect_a2_npu_ut_files(repo)
        e2e_files = [t for t in _A2_NPU_E2E_TESTS
                      if (repo / t).exists()]
        ts_print(f"[pre_ci] ut: NPU detected — adding {len(a2_npu_files)} "
                 f"A2 NPU UT files + {len(e2e_files)} E2E smoke test(s)")
    else:
        ts_print("[pre_ci] ut: no NPU detected — running CPU UT only "
                 "(A2 NPU UT + E2E smoke skipped)")

    # Read numpy constraint from triton-ascend metadata (mirror _check_mypy).
    # CI's lint image uses numpy 1.26.4 (constrained by triton-ascend);
    # main2main installs triton-ascend with --no-deps so system numpy is 2.x.
    target_numpy_spec = ""
    try:
        from packaging.requirements import Requirement
        reqs = _md.requires("triton-ascend") or []
        for req in reqs:
            if "extra" in req.lower():
                continue
            try:
                r = Requirement(req)
            except Exception:
                continue
            if r.name.lower() == "numpy":
                target_numpy_spec = ",".join(
                    f"{s.operator}{s.version}" for s in r.specifier)
                break
    except Exception as e:
        ts_print(f"[pre_ci] ut: failed to read triton-ascend numpy constraint ({e})")

    if not target_numpy_spec:
        ts_print("[pre_ci] ut: WARNING no triton-ascend numpy constraint — "
                 "using system numpy (may cause spurious dtype/shape failures)")

    # Build env: PYTHONPATH=ascend:vllm so vllm_ascend resolves to the
    # adapted working tree (editable install + PYTHONPATH prepends).
    env = os.environ.copy()
    if vllm_path:
        ascend_abs = str(repo.resolve())
        vllm_abs = str(Path(vllm_path).resolve())
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = (
            f"{ascend_abs}:{vllm_abs}:{existing}" if existing
            else f"{ascend_abs}:{vllm_abs}")
        ts_print(f"[pre_ci] ut: PYTHONPATH={ascend_abs}:{vllm_abs}")
    # CPU UT uses TORCH_DEVICE_BACKEND_AUTOLOAD=0 (conftest mocks torch_npu).
    # NPU tests need real torch_npu, so don't set autoload=0 when NPU is on.
    if npu_ok:
        env.update(_UT_NPU_ENV)
        env = _source_ascend_env(env)
    else:
        env.update(_UT_CPU_ENV)

    # Create venv with --system-site-packages, install numpy constraint.
    venv_dir: Path | None = None
    pytest_cmd = [pytest_bin]
    if vllm_path and target_numpy_spec:
        venv_dir = Path(tempfile.mkdtemp(prefix="ut_venv_"))
        ts_print(f"[pre_ci] ut: creating lint-equivalent venv at {venv_dir} "
                 f"(numpy{target_numpy_spec} from triton-ascend)")
        try:
            r = subprocess.run(
                [sys.executable, "-m", "venv", str(venv_dir), "--system-site-packages"],
                capture_output=True, text=True, timeout=180,
            )
        except subprocess.TimeoutExpired:
            ts_print("[pre_ci] ut: WARNING venv creation TIMED OUT (180s) — "
                     "falling back to system pytest")
            r = None  # type: ignore[assignment]
        if r is not None and r.returncode == 0:
            venv_python = venv_dir / "bin" / "python"
            # Install numpy constraint in venv (overrides system numpy 2.x).
            try:
                r2 = subprocess.run(
                    [str(venv_python), "-m", "pip", "install",
                     f"numpy{target_numpy_spec}"],
                    capture_output=True, text=True, timeout=300,
                )
            except subprocess.TimeoutExpired:
                ts_print("[pre_ci] ut: WARNING numpy install TIMED OUT — "
                         "falling back to system pytest")
                r2 = None  # type: ignore[assignment]
            if r2 is not None and r2.returncode != 0:
                ts_print(f"[pre_ci] ut: numpy install FAILED "
                         f"({r2.stderr.strip()[:300]}) — falling back to system pytest")
            elif r2 is not None:
                try:
                    vr = subprocess.run(
                        [str(venv_python), "-c", "import numpy; print(numpy.__version__)"],
                        capture_output=True, text=True, timeout=30,
                    )
                except subprocess.TimeoutExpired:
                    vr = None
                installed = vr.stdout.strip() if (vr and vr.returncode == 0) else "?"
                ts_print(f"[pre_ci] ut: venv numpy installed: {installed} "
                         f"(expected numpy{target_numpy_spec})")
                if installed.startswith("2."):
                    ts_print(f"[pre_ci] ut: WARNING numpy {installed} is 2.x — "
                             "results may contain spurious dtype/shape failures "
                             "(lint image uses numpy 1.26.4)")
                pytest_cmd = [str(venv_python), "-m", "pytest"]
        elif r is not None:
            ts_print(f"[pre_ci] ut: venv creation failed ({r.stderr.strip()[:200]}) — "
                     "using system pytest")

    try:
        # Run pytest batches in order.  Each batch is a separate subprocess
        # invocation — mirrors CI's run_pytest_batch (one pytest per group).
        # Stop-on-first-failure is disabled so we collect ALL failures across
        # all batches (gives adapter the full picture for fixing).
        ansi_re = re.compile(r"\x1b\[[0-9;]*m")
        failed_re = re.compile(r"^(FAILED|ERROR)\s+(\S+\.py::\S+)")

        def _run_batch(label: str, files: list[str], batch_timeout: int,
                       print_tail_lines: int = 50) -> tuple[list[str], str]:
            """Run one pytest batch.  Returns (violations, detail).

            violations: list of FAILED/ERROR summary lines (node IDs).
            detail: short status string.
            """
            if not files:
                return [], ""
            cmd = [*pytest_cmd, "-sv", "--color=yes", "--tb=short", "-q", *files]
            ts_print(f"\n[pre_ci] === pytest {label} ({len(files)} files) begin ===")
            try:
                r = subprocess.run(
                    cmd, cwd=str(repo), capture_output=True, text=True, env=env,
                    timeout=batch_timeout,
                )
            except subprocess.TimeoutExpired:
                ts_print(f"\n[pre_ci] ut: FAILED — {label} timed out "
                         f"after {batch_timeout}s")
                return ([f"{label} timed out after {batch_timeout}s"],
                        f"{label} timeout ({batch_timeout}s)")
            output = r.stdout + "\n" + r.stderr
            clean_output = ansi_re.sub("", output)
            ts_print(f"[pre_ci] === pytest {label} end (exit={r.returncode}) ===")
            tail = "\n".join(clean_output.splitlines()[-print_tail_lines:])
            ts_print(tail)

            if r.returncode == 0:
                ts_print(f"\n[pre_ci] ut: {label} OK ({len(files)} files clean)")
                return [], f"{label} clean ({len(files)} files)"

            seen: set[str] = set()
            violations: list[str] = []
            for line in clean_output.splitlines():
                m = failed_re.search(line.strip())
                if m and m.group(2) not in seen:
                    seen.add(m.group(2))
                    violations.append(line.strip())
            if violations:
                ts_print(f"\n[pre_ci] ut: {label} — {len(violations)} failure(s):")
                for v in violations[:20]:
                    ts_print(f"  {v}")
                if len(violations) > 20:
                    ts_print(f"  ... and {len(violations) - 20} more")
                return violations, f"{label}: {len(violations)} failure(s)"
            ts_print(f"\n[pre_ci] ut: {label} FAILED but no parseable lines")
            return ([clean_output[-2000:]],
                    f"{label} failed (no parseable failures)")

        # Batch 1: CPU UT (158 files, 30 min timeout)
        cpu_violations, cpu_detail = _run_batch(
            "CPU-UT", cpu_files, timeout_s)

        # Batch 2: A2 NPU UT (only when NPU available).  Uses NPU env
        # already set above (sourced Ascend toolkit, no autoload override).
        npu_violations: list[str] = []
        npu_detail = ""
        if npu_ok and a2_npu_files:
            npu_violations, npu_detail = _run_batch(
                "A2-NPU-UT", a2_npu_files, timeout_s)

        # Batch 3: A2 NPU E2E smoke (test_extract_hidden_states + future).
        # E2E tests load real models (~7 min each) — longer timeout, fewer
        # tail lines to avoid log spam.
        e2e_violations: list[str] = []
        e2e_detail = ""
        if npu_ok and e2e_files:
            e2e_violations, e2e_detail = _run_batch(
                "A2-NPU-E2E", e2e_files, _A2_NPU_E2E_TIMEOUT_S,
                print_tail_lines=80)

        all_violations = cpu_violations + npu_violations + e2e_violations
        details = [d for d in (cpu_detail, npu_detail, e2e_detail) if d]

        if not all_violations:
            total = len(cpu_files) + len(a2_npu_files) + len(e2e_files)
            ts_print(f"\n[pre_ci] ut: OK — all batches clean ({total} files)")
            return {"violations": [], "detail": f"UT clean ({total} files, "
                    f"cpu={len(cpu_files)}+npu_ut={len(a2_npu_files)}"
                    f"+e2e={len(e2e_files)})"}
        return {"violations": all_violations,
                "detail": f"{len(all_violations)} UT failure(s): "
                          f"{'; '.join(details)}"}
    finally:
        if venv_dir and venv_dir.exists():
            shutil.rmtree(venv_dir, ignore_errors=True)
            ts_print(f"[pre_ci] ut: destroyed temporary venv at {venv_dir}")


def _check_broken_imports(repo: Path, vllm_path: str | Path) -> dict:
    """Verify newly-added ``from vllm.X`` imports.

    1. Module must exist in the vllm tree (file or package dir).
    2. If the import is inside a ``vllm_version_is`` guard block, the line
       MUST carry ``# type: ignore[import-not-found]`` — mypy checks all
       static paths regardless of runtime guards.  No mypy needed here;
       this is a pure static check on the source text.
    """
    vllm_src = Path(vllm_path) / "vllm"
    added_lines = _get_added_lines(repo)
    violations: list[str] = []
    _indent_cache: dict[str, set[int]] = {}

    def _indent_width(line: str) -> int:
        return len(line) - len(line.lstrip())

    def _guarded_lines(fname: str) -> set[int]:
        """Return the set of line numbers inside a vllm_version_is guard."""
        if fname in _indent_cache:
            return _indent_cache[fname]
        fp = repo / fname
        if not fp.exists():
            _indent_cache[fname] = set()
            return set()
        lines = fp.read_text(encoding="utf-8").splitlines()
        guarded: set[int] = set()
        guard_stack: list[int] = []  # indent depths of active guards
        for lineno, raw in enumerate(lines, 1):
            line = raw.strip()
            indent = _indent_width(raw)
            # Pop guards that have ended (same or lower indent than guard start)
            while guard_stack and indent <= guard_stack[-1]:
                guard_stack.pop()
            if line.startswith(("if vllm_version_is(", "if not vllm_version_is(")):
                guard_stack.append(indent)
            if guard_stack:
                guarded.add(lineno)
        _indent_cache[fname] = guarded
        return guarded

    for entry in added_lines:
        line = entry["text"].strip()
        if not line.startswith("from vllm."):
            continue
        parts = line.replace(",", " ").split()
        if len(parts) < 2:
            continue
        mod = parts[1]
        if mod.startswith("vllm."):
            mod = mod[len("vllm."):]
        base = vllm_src / mod.replace(".", "/")
        exists = base.with_suffix(".py").exists() or (base / "__init__.py").exists()

        if not exists:
            violations.append(f"{entry['file']}:{entry['line_no']}: module not found — {line}")
            continue

        has_ignore = "# type: ignore[import-not-found]" in line
        if not has_ignore and int(entry["line_no"]) in _guarded_lines(entry["file"]):
            # Auto-fix: append the comment to the import line in the source file.
            # This is a purely mechanical fix — no logic change, no reason to
            # force an adapter retry.
            fpath = repo / entry["file"]
            if fpath.exists():
                orig_lines = fpath.read_text(encoding="utf-8").splitlines()
                lineno = int(entry["line_no"]) - 1  # 0-based
                if 0 <= lineno < len(orig_lines):
                    orig_lines[lineno] = orig_lines[lineno].rstrip() + "  # type: ignore[import-not-found]"
                    fpath.write_text("\n".join(orig_lines) + "\n", encoding="utf-8")
                    ts_print(f"[pre_ci] broken_imports: auto-fixed {entry['file']}:{entry['line_no']} "
                             f"(added # type: ignore[import-not-found])")

    return {"violations": violations}


def run_check(ascend_path: str | Path, release_tag: str,
              vllm_path: str | Path | None = None) -> dict:
    """Run pre-CI checks on the vllm-ascend working tree.

    Returns a dict with 'all_passed' (bool) and 'checks' (list of check results).
    If `vllm_path` is provided, also verifies that any new ``from vllm.X``
    imports in changed Python files reference modules that actually exist.
    """
    repo = Path(ascend_path)

    try:
        added_lines = _get_added_lines(repo)
        versions = _check_version_strings(added_lines, release_tag)
        temps = _check_temp_files(repo)
        imports = _check_broken_imports(repo, vllm_path) if vllm_path else {"violations": []}
    except subprocess.CalledProcessError as exc:
        return {
            "all_passed": False,
            "error": f"git command failed: {exc.stderr}",
            "checks": [],
        }

    checks: list[dict] = []
    all_passed = True

    version_ok = len(versions["mismatched"]) == 0
    checks.append({
        "name": "version_strings",
        "passed": version_ok,
        "detail": (
            f"{versions['new_calls_count']} new vllm_version_is() calls "
            f"all use {release_tag}"
            if version_ok
            else (
                f"{len(versions['mismatched'])} new vllm_version_is() calls "
                f"use wrong version (expected {release_tag})"
            )
        ),
        "mismatched": versions["mismatched"],
    })
    if not version_ok:
        all_passed = False

    temp_ok = len(temps["violations"]) == 0
    checks.append({
        "name": "temp_files",
        "passed": temp_ok,
        "detail": (
            "no temp files in repo"
            if temp_ok
            else f"{len(temps['violations'])} temp files found in repo"
        ),
        "violations": temps["violations"],
    })
    if not temp_ok:
        all_passed = False

    if vllm_path:
        import_ok = len(imports["violations"]) == 0
        checks.append({
            "name": "broken_imports",
            "passed": import_ok,
            "detail": (
                "all new vllm imports resolve to existing modules"
                if import_ok
                else f"{len(imports['violations'])} broken import(s): "
                     f"{'; '.join(imports['violations'])}"
            ),
            "violations": imports["violations"],
        })
        if not import_ok:
            all_passed = False

    return {"all_passed": all_passed, "checks": checks}
