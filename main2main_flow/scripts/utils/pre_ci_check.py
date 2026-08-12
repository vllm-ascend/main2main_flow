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

# test_schedule_body_matches_pinned_release_tag compares the copied
# BalanceScheduler.schedule() body against the pinned vLLM release tag, but
# vllm-ascend's copy deliberately tracks vllm MAIN (the drop-in replacement
# must match the installed scheduler) — the two diverge by design (the test's
# own docstring says so).  Upstream CI never executes this guard: vllm is
# installed from a wheel there, so `git show <tag>` is unreachable and the
# test skips.  main2main runs vllm as a git checkout WITH the tag fetched, so
# the test fires — and fails on BOTH batches, before any adaptation.
# Re-syncing the copy to the tag would revert upstream main's scheduler code,
# which is out of scope for main2main.  Excluded via -k (a no-op if upstream
# later renames/removes the test).
_BALANCE_TAG_BODY_TEST = "test_schedule_body_matches_pinned_release_tag"


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


def _check_fast_format(repo: Path) -> dict:
    """Quick ruff-check on the changed files via pre-commit (not full format.sh).

    The ruff-check hook auto-fixes what it can (isort/import order), which is
    treated as success — the tree is left clean.  Only violations that
    survive the auto-fix (e.g. F821 undefined name) fail the check.  This
    catches format mistakes inside the step's fix loop; previously they were
    only found by the reviewer or the final gate, and a fix round churned on
    them (run 31376860112: an isort violation in compiler_interface.py burned
    ~35 min of adapter retries).
    """
    if not shutil.which("pre-commit"):
        return {"violations": [], "detail": "pre-commit not installed", "skipped": True}
    py_files = subprocess.run(
        ["git", "diff", "HEAD", "--name-only", "--", "*.py"],
        cwd=str(repo), capture_output=True, text=True,
    ).stdout.splitlines()
    if not py_files:
        return {"violations": [], "detail": "no changed python files", "skipped": True}
    snapshot = subprocess.run(
        ["git", "diff", "HEAD", "--", "*.py"], cwd=str(repo),
        capture_output=True, text=True,
    ).stdout
    r = subprocess.run(
        ["pre-commit", "run", "ruff-check", "--files", *py_files],
        cwd=str(repo), capture_output=True, text=True,
    )
    output = (r.stdout + "\n" + r.stderr)
    if r.returncode == 0:
        return {"violations": [], "detail": "ruff-check OK"}
    if r.returncode == 2:
        # pre-commit infra error (env not ready etc.) — do not block the step.
        return {"violations": [], "detail": "ruff-check skipped (pre-commit error)", "skipped": True}
    after = subprocess.run(
        ["git", "diff", "HEAD", "--", "*.py"], cwd=str(repo),
        capture_output=True, text=True,
    ).stdout
    if after != snapshot:
        ts_print("[pre_ci] format: ruff-check auto-fixed the changed files")
        return {"violations": [], "detail": "ruff-check auto-fixed the changed files"}
    violations = [l for l in output.splitlines() if l.strip()][:20]
    return {"violations": violations,
            "detail": f"{len(violations)} ruff violation(s) (not auto-fixable)"}


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
# runners and are NOT part of the CPU-UT batch.
_CPU_UT_A2_RE = re.compile(r"tests/ut/.+/a2(/|$)")
_CPU_UT_A3_2_RE = re.compile(r"tests/ut/.+/a3_2(/|$)")


def _is_npu_convention_ut_path(rel_path: str) -> bool:
    """Mirror select_tests._route_ut_dir — True if path routes to NPU runner."""
    p = rel_path.replace("\\", "/")
    return bool(_CPU_UT_A2_RE.search(p) or _CPU_UT_A3_2_RE.search(p))


# a2-routed UTs that are too heavy for the batch (verified on CI:
# test_attention_v1_precision 500s, test_mla_precision 125s, and
# test_sfa_v1_precision hangs).  They get their own partition time on CI;
# the flow excludes them.
_A2_UT_EXCLUDE = ("test_attention_v1_precision.py", "test_mla_precision.py",
                  "test_sfa_v1_precision.py")

# a2-routed UTs that break at COLLECTION when imported after other files in
# the shared pytest process (verified: test_find_loaded_library.py errors at
# collection in the batch but passes standalone).  Run in their own process.
_A2_UT_ISOLATED = ("test_find_loaded_library.py",)


def _collect_a2_ut_files(repo: Path) -> list[str]:
    """Return a2-routed tests/ut paths (rel to repo), for the E2E batch.

    a2-routed UTs (tests/ut/**/a2/) test REAL NPU kernels — the CPU-UT gate
    can't cover them (it mocks torch_npu), so they ride along with the E2E
    runs on the real NPU.  Routing mirrors CI's runner_mapping
    (tests/ut/.+/a2), excluding the heavy precision files (see
    _A2_UT_EXCLUDE).  Reads test_config.yaml like _collect_cpu_ut_files.
    """
    a2_patterns: list[re.Pattern] = []
    config_path = repo / ".github/workflows/scripts/test_config.yaml"
    if config_path.exists():
        try:
            import yaml
            docs = list(yaml.safe_load_all(
                config_path.read_text(encoding="utf-8")))
            meta = docs[1] if len(docs) >= 2 and docs[1] else {}
            for pattern_str in ((meta or {}).get("runner_mapping", {}) or {}):
                if re.fullmatch(r"tests/ut/.+/a2", pattern_str):
                    a2_patterns.append(re.compile(pattern_str))
        except Exception as e:
            ts_print(f"[pre_ci] ut: failed to parse test_config.yaml ({e}), "
                     "using convention regex for a2 UTs")
            a2_patterns = []
    if not a2_patterns:
        a2_patterns = [re.compile(r"tests/ut/.+/a2(/|$)")]

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
                if not any(p.search(rel) for p in a2_patterns):
                    continue
                if f in _A2_UT_EXCLUDE:
                    continue
                files.append(rel)
    return files


def _collect_cpu_ut_files(repo: Path) -> list[str]:
    """Return CPU-routed tests/ut paths (rel to repo).

    Routes from vllm-ascend's OWN ``test_config.yaml`` — the same
    ``runner_mapping`` + ``skip_tests`` CI's select_tests.py reads — so the
    set tracks CI automatically when routing changes (new NPU directories,
    new skip entries).  Reading the config is read-only; vllm-ascend is
    never modified.  Falls back to the convention regexes when the config
    is missing or unparseable (old checkouts).
    """
    skip_tests: set[str] = set()
    npu_patterns: list[re.Pattern] = []
    config_path = repo / ".github/workflows/scripts/test_config.yaml"
    if config_path.exists():
        try:
            import yaml
            docs = list(yaml.safe_load_all(
                config_path.read_text(encoding="utf-8")))
            modules = docs[0] or []
            meta = docs[1] if len(docs) >= 2 and docs[1] else {}
            for module in modules:
                for s in module.get("skip_tests", []):
                    skip_tests.add(str(s).rstrip("/"))
            for pattern_str in ((meta or {}).get("runner_mapping", {}) or {}):
                if pattern_str.startswith("tests/ut"):
                    npu_patterns.append(re.compile(pattern_str))
            if npu_patterns:
                ts_print(f"[pre_ci] ut: routing from test_config.yaml "
                         f"({len(npu_patterns)} NPU pattern(s), "
                         f"{len(skip_tests)} skip entry(s))")
        except Exception as e:
            ts_print(f"[pre_ci] ut: failed to parse test_config.yaml ({e}), "
                     "falling back to convention regexes")
            npu_patterns = []

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
                if skip_tests and rel in skip_tests:
                    continue
                if npu_patterns:
                    if any(p.search(rel) for p in npu_patterns):
                        continue
                elif _is_npu_convention_ut_path(rel):
                    continue
                files.append(rel)
    return files


def _check_ut(repo: Path, vllm_path: str | Path | None = None,
              vllm_release_path: str | Path | None = None,
              release_tag: str = "",
              timeout_s: int = 1800) -> dict:
    """Run the CPU-UT batch, aligned with CI's single-process execution.

    Runs the same CPU-routed tests/ut/* files as CI's CPU runner
    (linux-amd64-cpu-8-hk), but on whatever machine main2main runs on —
    including the A2 NPU runner.  Mirrors vllm-ascend's
    ``run_selected_tests.sh`` cpu-ut batch: ALL files in ONE pytest
    process (CI runs 2044 tests in ~44s; per-file subprocess isolation
    cost ~5 min per version).  The key mechanism:

    **Fake npu-smi on the PATH**: vllm-ascend's tests/ut/conftest.py
    checks ``npu-smi info`` to decide whether to mock torch_npu.
    On the A2 runner npu-smi succeeds, so conftest would NOT mock and
    CPU UT cases would hit real NPU ops (e.g. ``swiglustep: N=4 must
    be multiple of 8``).  We prepend a temp dir with a fake
    ``npu-smi`` script (exit 1) to the child's PATH — conftest then
    takes the mock path, exactly like CI's CPU runner.

    Runs the batch against BOTH vllm versions: the target main checkout
    (``vllm_path``) AND the pinned release (``vllm_release_path``, e.g.
    v0.26.0).  vllm-ascend carries ``vllm_version_is("<release_tag>")``
    guards — a fix that passes on main can break the release branch, so
    both must pass.  Violations are aggregated with the version labeled.

    Release-batch specifics:
    - ``VLLM_VERSION`` is set from the release tag: a raw git worktree has
      no build-generated ``vllm/_version.py``, so ``vllm.__version__`` is
      "dev" and every module-level ``vllm_version_is()`` guard raises at
      collection (all 166 files failed before this fix).
    - ``test_vllm_version_is`` is excluded on the release batch only — it
      unit-tests the env-var fallback with a mocked env, which the
      real ``VLLM_VERSION`` override conflicts with.
    - ``test_schedule_body_matches_pinned_release_tag`` is excluded on BOTH
      batches — see ``_BALANCE_TAG_BODY_TEST``.

    Env mirrors CI: venv with --system-site-packages + numpy==1.26.4
    (from triton-ascend metadata) + PYTHONPATH=ascend:vllm.  torch_npu
    is mocked by conftest, so the venv python's C-extension issue that
    broke A2-NPU-UT in PR #13657 does not apply here.

    Returns dict with ``violations`` (failing test node IDs) and
    ``detail``.  Empty violations + non-skipped → pass.
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
             f"(per-file isolation, NPU-convention a2/ and a3_2/ excluded)")

    # Read numpy constraint from triton-ascend metadata (mirror _check_mypy).
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

    # Create venv with --system-site-packages, install numpy constraint.
    venv_dir: Path | None = None
    pytest_cmd = [pytest_bin]
    if (vllm_path or vllm_release_path) and target_numpy_spec:
        venv_dir = Path(tempfile.mkdtemp(prefix="ut_venv_"))
        ts_print(f"[pre_ci] ut: creating venv at {venv_dir} "
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
                pytest_cmd = [str(venv_python), "-m", "pytest"]
        elif r is not None:
            ts_print(f"[pre_ci] ut: venv creation failed ({r.stderr.strip()[:200]}) — "
                     "using system pytest")

    # Fake npu-smi: prepend a temp dir with an `npu-smi` that exits 1, so
    # tests/ut/conftest.py takes the mock path (as on CI's CPU runner).
    fake_bin_dir = Path(tempfile.mkdtemp(prefix="ut_fake_bin_"))
    ansi_re = re.compile(r"\x1b\[[0-9;]*m")
    failed_re = re.compile(r"^(FAILED|ERROR)\s+(\S+\.py::\S+)")

    try:
        fake_npu_smi = fake_bin_dir / "npu-smi"
        fake_npu_smi.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake_npu_smi.chmod(0o755)

        # Versions to test: main checkout + pinned release (if available).
        # Third element: VLLM_VERSION to force for the batch.  A release
        # worktree is a raw git checkout without the build-generated
        # vllm/_version.py, so vllm.__version__ falls back to "dev" — an
        # invalid packaging version.  Every module-level vllm_version_is()
        # guard in vllm-ascend (e.g. npu_communicator.py at import) then
        # raises at collection and ALL test files fail.  vllm_version_is()
        # honors the VLLM_VERSION env var (x.y.z), so pin it to the release.
        versions: list[tuple[str, Path | None, str]] = [
            ("main", Path(vllm_path) if vllm_path else None, ""),
        ]
        if vllm_release_path:
            rel_tag = release_tag
            if not rel_tag:
                # Fallback: read the pin from vllm-ascend's tracking file (the
                # same source initialize() used to create the worktree).
                pin_file = repo / ".github" / "vllm-release-tag.commit"
                if pin_file.exists():
                    rel_tag = pin_file.read_text(encoding="utf-8").strip()
            versions.append((rel_tag or "release",
                             Path(vllm_release_path),
                             rel_tag.lstrip("v")))

        all_violations: list[str] = []
        details: list[str] = []
        all_files_clean = True
        for label, vpath, vllm_version in versions:
            if vpath is None:
                ts_print(f"\n[pre_ci] ut: {label}: no vllm path, skipped")
                continue
            env = os.environ.copy()
            ascend_abs = str(repo.resolve())
            vllm_abs = str(vpath.resolve())
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (
                f"{ascend_abs}:{vllm_abs}:{existing}" if existing
                else f"{ascend_abs}:{vllm_abs}")
            if vllm_version:
                env["VLLM_VERSION"] = vllm_version
            env["TORCH_DEVICE_BACKEND_AUTOLOAD"] = "0"
            env["VLLM_WORKER_MULTIPROC_METHOD"] = "spawn"
            env["PATH"] = f"{fake_bin_dir}:{env.get('PATH', '')}"

            ts_print(f"\n[pre_ci] ut: === batch [{label}] "
                     f"PYTHONPATH={ascend_abs}:{vllm_abs} ===")

            # CI-aligned: run all files in ONE pytest process, exactly like
            # vllm-ascend's run_selected_tests.sh cpu-ut batch (2044 tests in
            # ~44s on CI).  The previous per-file subprocess isolation cost
            # ~5 min per version (python+torch import per file); a single
            # process shares those imports and runs in ~2 min.
            #
            # Exception: files known to pollute the shared process get their
            # own subprocess.  Verified on the A2 env: test_batch_invariant.py
            # installs a global torch.library.Library monkeypatch that breaks
            # test_gdn_layerwise_kv.py when run in the same process (the
            # original per-file isolation existed for exactly this pair).
            # test_vocab_parallel_embedding.py assigns module-level
            # parallel_state._MLP_TP/_OTP = MagicMock without cleanup,
            # polluting test_linear.py / test_gdn_layerwise_kv.py in the
            # same process (verified on A2, run 2026-08-12).
            isolated = [f for f in cpu_files
                        if f.endswith(("test_batch_invariant.py",
                                       "test_vocab_parallel_embedding.py"))]
            batch = [f for f in cpu_files if f not in isolated]

            exclude_expr = f"not {_BALANCE_TAG_BODY_TEST}"
            if vllm_version:
                # test_vllm_version_is unit-tests the VLLM_VERSION env
                # fallback with a mocked env; the release batch sets
                # VLLM_VERSION for real, so its __version__-fallback
                # assertions can't hold there.  Main batch runs it as-is.
                exclude_expr += " and not test_vllm_version_is"

            runs: list[tuple[str, subprocess.CompletedProcess]] = []
            try:
                # --continue-on-collection-errors: a single file that fails
                # to import (e.g. an env-specific ModuleNotFoundError) must
                # NOT abort the whole batch and mask every other test —
                # the batch is one pytest process for all files (run
                # 31563761175: sfa_pd_rd2h collection error hid 8 real
                # regressions that PR CI then exposed).
                # -p pytest_ascend_examples: PYTHONPATH=<ascend>:<vllm>
                # makes vllm's regular examples/ package shadow ascend's
                # namespace examples/ — pre-register the ascend dir so the
                # batch matches real CI (vllm installed, no examples/ on
                # sys.path).
                rr = subprocess.run(
                    [*pytest_cmd, "-q", "--tb=short", "--no-header",
                     "--continue-on-collection-errors",
                     "-p", "main2main_flow.scripts.utils.pytest_ascend_examples",
                     *batch, "-k", exclude_expr],
                    cwd=str(repo), capture_output=True, text=True,
                    env=env, timeout=1200,
                )
                runs.append(("batch", rr))
            except subprocess.TimeoutExpired:
                ts_print(f"[pre_ci] ut: [{label}] batch TIMEOUT(1200s)")
                all_files_clean = False
                details.append(f"{label}/batch: TIMEOUT(1200s)")
            for f in isolated:
                try:
                    rr = subprocess.run(
                        [*pytest_cmd, "-q", "--tb=short", "--no-header", f],
                        cwd=str(repo), capture_output=True, text=True,
                        env=env, timeout=300,
                    )
                    runs.append((f, rr))
                except subprocess.TimeoutExpired:
                    ts_print(f"[pre_ci] ut: [{label}] {f} TIMEOUT(300s)")
                    all_files_clean = False

            for name, rr in runs:
                clean = ansi_re.sub("", rr.stdout + rr.stderr)
                seen: set[str] = set()
                for line in clean.splitlines():
                    m = failed_re.search(line.strip())
                    if m and m.group(2) not in seen:
                        seen.add(m.group(2))
                        all_violations.append(f"[{label}] {line.strip()}")
                if rr.returncode != 0:
                    all_files_clean = False
                    if not seen:
                        # No parseable failures — dump output tail.
                        all_violations.append(
                            f"[{label}] {name}: exit={rr.returncode} — "
                            f"{clean[-500:]}")
                summary_m = re.search(
                    r"((?:\d+ failed, )?\d+ passed[^\n]*)", clean)
                summary = (summary_m.group(1) if summary_m
                           else f"exit={rr.returncode}")
                details.append(f"{label}/{name}: {summary}")
                ts_print(f"[pre_ci] ut: [{label}/{name}] {summary}")

            # a2-routed UTs (real NPU kernels, tests/ut/**/a2/): the CPU batch
            # above mocks torch_npu via the fake npu-smi, so these run
            # separately on the REAL device — the main2main runner has NPU and
            # CI routes them to a2 runners too.  One pytest process per
            # version (per-file startup dominates; the batch itself is
            # ~1-2 min).  Heavy precision files are excluded — CI gives them
            # their own partition budget (test_attention_v1_precision ~500s).
            a2_files = _collect_a2_ut_files(repo)
            if a2_files:
                a2_env = env.copy()
                a2_env["PATH"] = a2_env["PATH"].replace(f"{fake_bin_dir}:", "")
                # Match vllm-ascend CI's "with device" jobs exactly:
                #  - NO TORCH_DEVICE_BACKEND_AUTOLOAD=0 (that is only set on
                #    CI's CPU/without-device step).  torch_npu must load via
                #    autoload — that path registers the inductor npu backend;
                #    with AUTOLOAD=0 the compile path fails with
                #    "Device npu not supported".
                #  - CI container env vars (container-level in
                #    _selected_tests.yaml).
                a2_env.pop("TORCH_DEVICE_BACKEND_AUTOLOAD", None)
                a2_env.setdefault("VLLM_LOGGING_LEVEL", "ERROR")
                a2_env.setdefault("VLLM_USE_MODELSCOPE", "True")
                a2_env.setdefault("HF_HUB_OFFLINE", "1")
                a2_env.setdefault("MAX_JOBS", "4")
                isolated_a2 = [f for f in a2_files
                               if f.rsplit("/", 1)[-1] in _A2_UT_ISOLATED]
                batch_a2 = [f for f in a2_files if f not in isolated_a2]
                ts_print(f"\n[pre_ci] ut: === a2 batch [{label}] "
                         f"({len(batch_a2)} files + "
                         f"{len(isolated_a2)} isolated, real NPU) ===")

                a2_runs: list[tuple[str, subprocess.CompletedProcess]] = []
                # Use the SYSTEM python (like the e2e runs), not the numpy
                # venv: the a2 UTs run on the real NPU and need the CANN
                # bindings (acl etc.) that the venv python can't see.
                a2_pytest = [sys.executable, "-m", "pytest"]
                try:
                    rr = subprocess.run(
                        [*a2_pytest, "-q", "--tb=short", "--no-header",
                         "--continue-on-collection-errors", *batch_a2],
                        cwd=str(repo), capture_output=True, text=True,
                        env=a2_env, timeout=1200,
                    )
                    a2_runs.append(("a2", rr))
                except subprocess.TimeoutExpired:
                    ts_print(f"[pre_ci] ut: [{label}/a2] TIMEOUT(1200s)")
                    all_files_clean = False
                    details.append(f"{label}/a2: TIMEOUT(1200s)")
                for f in isolated_a2:
                    try:
                        rr = subprocess.run(
                            [*a2_pytest, "-q", "--tb=short", "--no-header", f],
                            cwd=str(repo), capture_output=True, text=True,
                            env=a2_env, timeout=300,
                        )
                        a2_runs.append((f, rr))
                    except subprocess.TimeoutExpired:
                        ts_print(f"[pre_ci] ut: [{label}] {f} TIMEOUT(300s)")
                        all_files_clean = False

                for name, rr in a2_runs:
                    clean = ansi_re.sub("", rr.stdout + rr.stderr)
                    a2_seen: set[str] = set()
                    for line in clean.splitlines():
                        m = failed_re.search(line.strip())
                        if m and m.group(2) not in a2_seen:
                            a2_seen.add(m.group(2))
                            all_violations.append(f"[{label}/a2] {line.strip()}")
                    if rr.returncode != 0:
                        all_files_clean = False
                        if not a2_seen:
                            all_violations.append(
                                f"[{label}/a2] {name}: exit={rr.returncode} — "
                                f"{clean[-500:]}")
                    summary_m = re.search(
                        r"((?:\d+ failed, )?\d+ passed[^\n]*)", clean)
                    summary = (summary_m.group(1) if summary_m
                               else f"exit={rr.returncode}")
                    details.append(f"{label}/a2/{name}: {summary}")
                    ts_print(f"[pre_ci] ut: [{label}/a2/{name}] {summary}")

        if all_files_clean:
            ts_print(f"\n[pre_ci] ut: OK — all {len(cpu_files)} files clean "
                     f"on all versions")
            return {"violations": [],
                    "detail": f"UT clean ({len(cpu_files)} files × "
                              f"{len(versions)} versions, single-process "
                              f"batch)"}
        ts_print(f"\n[pre_ci] ut: {len(all_violations)} failure(s):")
        for v in all_violations[:20]:
            ts_print(f"  {v}")
        if len(all_violations) > 20:
            ts_print(f"  ... and {len(all_violations) - 20} more")
        return {"violations": all_violations,
                "detail": f"{len(all_violations)} UT failure(s): "
                          f"{'; '.join(details)}"}
    finally:
        if venv_dir and venv_dir.exists():
            shutil.rmtree(venv_dir, ignore_errors=True)
        if fake_bin_dir.exists():
            shutil.rmtree(fake_bin_dir, ignore_errors=True)
            ts_print("[pre_ci] ut: removed fake npu-smi dir")


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

    fmt = _check_fast_format(repo)
    fmt_ok = len(fmt["violations"]) == 0
    checks.append({
        "name": "format",
        "passed": fmt_ok,
        "detail": fmt["detail"],
        "violations": fmt["violations"],
    })
    if not fmt_ok:
        all_passed = False

    return {"all_passed": all_passed, "checks": checks}
