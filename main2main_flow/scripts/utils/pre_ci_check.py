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

import ast
import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from main2main_flow.scripts.utils.utils import (FORMAT_TIMEOUT_RC,
                                                run_format_sh, run_git,
                                                pip_install_with_fallback,
                                                ts_print, WORKSPACE_DIR)
from main2main_flow.scripts.utils.ut_check import (check_ut as _check_ut,
                                                   _numpy_drifted,
                                                   _triton_numpy_spec,
                                                   _venv_numpy_version)  # noqa: E402

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

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

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
    codespell typos, typos, etc.

    Status is made TRUTHFUL by re-running format.sh after a failure: the
    pre-commit --fix hooks exit 1 after auto-fixing (they want a re-run to
    confirm), so a single exit != 0 is ambiguous.  Second run exit 0
    -> auto-fix-only, pass.  Second run still failing -> real residual
    errors; the FULL cleaned output is handed over (the output is only
    tens of lines — excerpting it lost the violation, run 33784514899).
    """
    fmt_script = repo / "format.sh"
    if not fmt_script.exists():
        ts_print("\n[pre_ci] format: SKIPPED — format.sh not found")
        return {"violations": [], "detail": "format.sh not found", "skipped": True}
    if not shutil.which("pre-commit"):
        ts_print("\n[pre_ci] format: SKIPPED — pre-commit not installed, all lint checks bypassed!")
        return {"violations": [], "detail": "pre-commit not installed", "skipped": True}

    def _run_once(tag: str) -> tuple[int, str, str]:
        ts_print(f"\n[pre_ci] === format.sh output begin ({tag}) ===")
        # run_format_sh echoes the output live; only collect it here for
        # violation extraction and for the final completed-process record.
        rr = run_format_sh(repo)
        out = _ANSI_RE.sub("", (rr.stdout + "\n" + rr.stderr))
        if not out.strip():
            ts_print("(no output)")
        ts_print(f"[pre_ci] === format.sh output end (exit={rr.returncode}) ===")
        diff_after = subprocess.run(
            ["git", "diff", "--stat"], cwd=str(repo), capture_output=True,
            text=True,
        ).stdout.strip()
        if diff_after:
            ts_print(f"[pre_ci] format.sh modified files in working tree:\n{diff_after}")
        return rr.returncode, out, diff_after

    rc, output, diff_after = _run_once("run 1")
    if rc == 0:
        ts_print("\n[pre_ci] format: OK")
        return {"violations": [], "detail": "format.sh OK"}
    if rc == FORMAT_TIMEOUT_RC:
        # The hook cache / store lock is wedged, not the adapter's code.
        # A second run would hang for the same timeout again, so stop here
        # and hand the streamed log tail (which names the last hook) over.
        ts_print("\n[pre_ci] format: TIMED OUT — not retrying")
        violations = [ln for ln in output.splitlines() if ln.strip()]
        violations.append("format.sh timed out; see the last hook in the log")
        return {"violations": violations,
                "detail": "format.sh TIMED OUT (exit=124) — not retried"}
    # exit != 0: auto-fix hooks may have fixed files (they exit 1 to ask
    # for a re-run).  Re-run to separate "fixed, clean now" from "real
    # residual errors".
    rc2, output2, diff_after2 = _run_once("run 2 (post auto-fix confirm)")
    if rc2 == 0:
        ts_print("\n[pre_ci] format: OK (auto-fixed on the first run)")
        return {"violations": [],
                "detail": "format.sh OK (auto-fixed on first run)"}
    # Still failing after the auto-fix pass — real residual lint errors.
    # Hand over the FULL cleaned output: excerpting it is how an E402 was
    # lost (run 33784514899).  Prefix-normalized lines (::error::,
    # ##[error], ANSI) are stripped so the adapter sees clean violations.
    full = output if rc2 == rc and not diff_after2 else output2
    violations = [re.sub(r'^#{0,2}\[error\]\s*|^::error::\s*', '', ln)
                  for ln in full.splitlines() if ln.strip()]
    ts_print(f"\n[pre_ci] format: FAILED — {len(violations)} line(s) from "
             f"format.sh output (exit={rc2})")
    return {"violations": violations,
            "detail": f"format.sh FAILED (exit={rc2}) — residual lint errors "
                      f"after auto-fix pass; full output in violations"}


def _changed_test_py_files(repo: Path) -> list[str]:
    """tests/examples .py files changed since the adaptation base.

    Uses ``upstream/main`` when available (covers committed step changes +
    uncommitted gate-fix edits), falls back to ``HEAD`` (uncommitted only).
    """
    base = "upstream/main"
    r = subprocess.run(
        ["git", "rev-parse", "--verify", base],
        cwd=str(repo), capture_output=True, text=True)
    if r.returncode != 0:
        base = "HEAD"
    diff = subprocess.run(
        ["git", "diff", base, "--name-only", "--", "tests/", "examples/"],
        cwd=str(repo), capture_output=True, text=True)
    return [f for f in diff.stdout.splitlines()
            if f.endswith(".py") and f.startswith(("tests/", "examples/"))]


_MYPY_VENV_ENV = "MAIN2MAIN_MYPY_VENV"
_MYPY_VENV_MARKER = "m2m_meta.json"


def _mypy_base_dir() -> Path:
    """Home of the persistent mypy lint venv (mirrors ut_check's ut_venv)."""
    env = os.environ.get(_MYPY_VENV_ENV, "")
    return Path(env) if env else WORKSPACE_DIR / "mypy_venv"


def _ensure_mypy_venv(target_numpy_spec: str) -> tuple[Path | None, str]:
    """Create-or-reuse the persistent lint venv; return (venv_dir, venv_python).

    Mirrors ut_check's _ensure_ut_venv: the venv lives across pre_ci
    attempts and steps (the runner is ephemeral, so no end-of-run cleanup
    is needed).  Reuse requires bin/python to exist AND the numpy spec in
    the marker to match triton-ascend's current constraint AND the
    actually importable numpy to satisfy it — the marker can look healthy
    while the venv never owned its numpy (pip "already satisfied" behind
    --system-site-packages) and drifts with system upgrades, rebuilding
    instead of linting wrong (run 38220121641).  Any failure falls back
    to the system mypy (returns (None, "")).
    """
    venv_dir = _mypy_base_dir()
    venv_python = venv_dir / "bin" / "python"
    if venv_python.exists():
        reason = ""
        try:
            meta = json.loads((venv_dir / _MYPY_VENV_MARKER).read_text(
                encoding="utf-8"))
            if meta.get("numpy_spec") != target_numpy_spec:
                reason = (f"numpy spec mismatch ({meta.get('numpy_spec')!r} "
                          f"!= {target_numpy_spec!r})")
        except Exception:
            reason = "marker unreadable"
        if not reason and target_numpy_spec:
            # Probe the ACTUAL numpy, not just the marker: e2e's vllm
            # install upgraded system numpy 1.26.4 -> 2.3.5 mid-run and the
            # lint venv drifted with it for three rounds (38220121641).
            installed = _venv_numpy_version(venv_python)
            if _numpy_drifted(target_numpy_spec, installed):
                reason = (f"installed numpy {installed or 'MISSING'} "
                          f"violates numpy{target_numpy_spec}")
        if reason:
            ts_print(f"[pre_ci] mypy: persistent venv {reason} — recreating")
            shutil.rmtree(venv_dir, ignore_errors=True)
        else:
            ts_print(f"[pre_ci] mypy: reusing persistent venv at {venv_dir}")
            return venv_dir, str(venv_python)

    ts_print(f"[pre_ci] mypy: creating persistent lint venv at {venv_dir} "
             f"(numpy{target_numpy_spec} from triton-ascend, no vllm package)")
    try:
        venv_dir.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir),
             "--system-site-packages"],
            capture_output=True, text=True, timeout=180,
        )
        if r.returncode != 0:
            ts_print("[pre_ci] mypy: WARNING venv creation FAILED — "
                     "falling back to system mypy")
            return None, ""
        if target_numpy_spec:
            # A failed install silently leaves system numpy 2.x in the venv,
            # reproducing the false positives — treat it as no venv.
            # --ignore-installed: behind --system-site-packages a satisfying
            # system numpy turns this into a pip no-op ("already satisfied")
            # and the venv never owns its pin — the e2e vllm install then
            # upgrades system numpy out from under it (run 38220121641:
            # 1.26.4 -> 2.3.5 mid-run).  Forcing the wheel into the venv
            # makes the pin immune to system-side flips.
            try:
                r2 = pip_install_with_fallback(
                    venv_python,
                    ["--ignore-installed", f"numpy{target_numpy_spec}",
                     "--no-build-isolation"])
            except subprocess.TimeoutExpired:
                ts_print("[pre_ci] mypy: WARNING numpy install TIMED OUT — "
                         "falling back to system mypy")
                r2 = None
            if r2 is not None and r2.returncode != 0:
                ts_print(f"[pre_ci] mypy: WARNING numpy install FAILED "
                         f"({r2.stderr.strip()[:200]}) — falling back "
                         "to system mypy")
                return None, ""
            # pip exiting 0 is not proof the wheel landed in the venv;
            # the probe catches any remaining silent no-op.
            if _numpy_drifted(target_numpy_spec,
                              _venv_numpy_version(venv_python)):
                ts_print(f"[pre_ci] mypy: WARNING post-install numpy probe "
                         f"violates numpy{target_numpy_spec} — falling "
                         "back to system mypy")
                shutil.rmtree(venv_dir, ignore_errors=True)
                return None, ""
        (venv_dir / _MYPY_VENV_MARKER).write_text(
            json.dumps({"numpy_spec": target_numpy_spec}),
            encoding="utf-8")
        return venv_dir, str(venv_python)
    except subprocess.TimeoutExpired:
        ts_print("[pre_ci] mypy: WARNING venv creation TIMED OUT (180s) — "
                 "falling back to system mypy")
        return None, ""


_MYPY_CACHE_ROOT = (Path.home() / ".cache"
                    / f"main2main-mypy-a3-16-{platform.machine()}")


def _mypy_cache_key(repo: Path, vllm_root: str | Path | None,
                    numpy_spec: str = "") -> str | None:
    """Bucket key over the inputs mypy's own cache does NOT track.

    mypy 1.11.1 (the repo's pin) reuses green entries when its cache
    validity checks should have invalidated them:

    * removing a per-module ``ignore_missing_imports`` override from
      mypy.ini does not invalidate cached "module resolved" results (run
      38943025557's discarded adapter branch added one for
      ``vllm.transformers_utils.configs.glm5_next``; run 38996027002's
      gate then reused the green entry although the module was gone from
      the tree);
    * switching PYTHONPATH to a vllm tree where the imported module no
      longer exists does not invalidate them either.

    Partition the persistent cache by mypy.ini bytes, the vllm tree HEAD
    and the numpy constraint (installed numpy types leak into results).
    Returns None when an input is unreadable — callers must then run with
    ``--no-incremental``, never fall back to a shared directory.
    """
    if vllm_root is None:
        return None
    try:
        ini = (repo / "mypy.ini").read_bytes()
    except OSError:
        return None
    try:
        r = subprocess.run(["git", "-C", str(vllm_root), "rev-parse", "HEAD"],
                           capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    head = r.stdout.strip()
    if r.returncode != 0 or not head:
        return None
    digest = hashlib.sha256(ini + b"\0" + head.encode() + b"\0"
                            + numpy_spec.encode())
    return digest.hexdigest()[:12]


def _check_mypy(repo: Path, vllm_path: str | Path | None = None,
                vllm_release_path: str | Path | None = None, *,
                fresh_cache: bool = False,
                targets: tuple[str, ...] | None = None,
                filter_sigs: tuple[str, ...] | None = None,
                python_versions: tuple[str, ...] | None = None) -> dict:
    """Run mypy with the same core command and environment as vllm-ascend's CI.

    Mirrors the CI pre-commit job's "Run mypy" step: for each python version
    in ``3.10/3.11/3.12`` (the matrix CI runs), runs::

        PYTHONPATH=<vllm_path> mypy --follow-imports skip --check-untyped-defs \\
            --python-version <X.Y> --exclude _cann_ops_custom/ \\
            vllm_ascend examples tests

    ``PYTHONPATH=<vllm_path>`` points mypy at vllm source (CI uses
    ``./vllm-empty`` at verified commit). ``--exclude _cann_ops_custom/``
    handles the build artifact dir that CI's lint image doesn't have.

    Dual-version validation: when ``vllm_release_path`` (a git worktree of
    the pinned release tag, e.g. v0.28.0) is given, the SAME sources are
    also type-checked against the release tree.  CI's pre-commit runs mypy
    on the main pin only, so release-only signature drift (a dataclass
    field required by the release base class but absent upstream) surfaces
    there as e2e TypeErrors — one extra mypy invocation catches it statically
    (PR #16296: missing ``max_seq_len_np`` killed 7 release-lane e2e jobs).
    The release pass runs at 3.10 only: it adds a different *tree*, not a
    different *grammar*; python-version-specific breakage is fully covered
    by the main pass's 3-version loop.

    Cache isolation: the persistent incremental cache is bucketed per
    (mypy.ini, vllm HEAD, numpy spec) so a verdict produced against one
    tree/config cannot leak into another (``_mypy_cache_key``).
    ``fresh_cache=True`` disables incremental reuse entirely — the
    push-time gate certifies the final diff, so it must not trust a cache
    written by an earlier round whose mypy.ini edit may have been
    discarded in between (PR 19089's false green).

    ``targets`` overrides the checked paths (default: ``vllm_ascend`` plus
    changed tests/examples).  With ``filter_sigs`` only output lines
    containing one of the given substrings count as violations and drive
    the result — used by the gate's full-tests import-resolution pass,
    where this runner's mock-pattern noise is expected but a missing
    ``vllm.*`` module is not.  ``python_versions`` overrides the main
    tree's version matrix.

    Returns all errors mypy reports across all python versions - no
    added-line filtering.  CI mypy is the source of truth; if it fails,
    the adaptation must be fixed.
    """
    mypy = shutil.which("mypy")
    if not mypy:
        return {"violations": [], "detail": "mypy not installed", "skipped": True}

    base_env = os.environ.copy()
    if vllm_path:
        vllm_abs = str(Path(vllm_path).resolve())
        existing = base_env.get("PYTHONPATH", "")
        base_env["PYTHONPATH"] = f"{vllm_abs}:{existing}" if existing else vllm_abs
        ts_print(f"\n[pre_ci] mypy: PYTHONPATH includes vllm source: {vllm_abs}")

    # CI's lint image runs mypy in a clean environment: no vllm package
    # installed (mypy uses PYTHONPATH for vllm source), numpy 1.26.4
    # (constrained by triton-ascend's metadata, installed WITHOUT --no-deps).
    #
    # main2main runner has vllm installed (target commit, for e2e) and numpy
    # 2.x (workflow installs triton-ascend with --no-deps, skipping the numpy
    # constraint).  This causes ~68 spurious mypy errors.
    #
    # Fix: run mypy inside a persistent venv with --system-site-packages
    # (inherits mypy, mypy.ini, triton-ascend, etc. from the system) holding
    # only the numpy version that triton-ascend constrains (dynamically read
    # from metadata, not hardcoded), and NO vllm (so mypy falls back to
    # PYTHONPATH).  This reproduces the CI lint image's mypy environment;
    # unlike the temp venv it replaces, it is built once and reused across
    # pre_ci attempts and steps (_ensure_mypy_venv).
    #
    # Verified: venv + numpy==1.26.4 + no vllm + PYTHONPATH=vllm source
    #   -> 0 errors (matches clean lint image exactly).
    target_numpy_spec = _triton_numpy_spec(label="mypy")

    if not target_numpy_spec:
        ts_print("[pre_ci] mypy: WARNING no triton-ascend numpy constraint found - "
                 "using system numpy (may report spurious [var-annotated] errors, "
                 "see known numpy 2.x issue)")

    # Persistent lint venv (created once, reused across attempts and steps).
    mypy_cmd = [mypy]  # default: use system mypy
    venv_python = ""
    if vllm_path and target_numpy_spec:
        _, venv_python = _ensure_mypy_venv(target_numpy_spec)
    if venv_python:
        # Re-report the actual numpy: _ensure_mypy_venv already probed it
        # and rebuilt on drift, so this stays visible in the log either
        # way and still warns if the rebuild itself failed.
        installed = _venv_numpy_version(venv_python) or "?"
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
        mypy_cmd = [venv_python, "-m", "mypy"]
        ts_print(f"[pre_ci] mypy: using venv mypy via {venv_python} -m mypy")

    # Persistent cache root, bucketed per tree by _mypy_cache_key: mypy's
    # own invalidation misses mypy.ini overrides and the identity of the
    # vllm tree behind PYTHONPATH (see the helper).  The push-time gate
    # passes fresh_cache=True: it certifies the final diff, so it must not
    # reuse a verdict produced by an earlier — possibly discarded — round
    # (PR 19089's false green).
    def _cache_env(env: dict[str, str], vllm_root: str | Path | None,
                   label: str) -> tuple[dict[str, str], bool]:
        """Return (env with this tree's cache bucket, incremental_ok)."""
        key = None if fresh_cache else _mypy_cache_key(
            repo, vllm_root, target_numpy_spec)
        if key is None:
            env.pop("MYPY_CACHE_DIR", None)
            why = ("fresh_cache" if fresh_cache
                   else "cache key unavailable (mypy.ini / vllm HEAD)")
            ts_print(f"[pre_ci] mypy [{label}]: incremental cache disabled "
                     f"({why}) -> --no-incremental")
            return env, False
        env["MYPY_CACHE_DIR"] = str(_MYPY_CACHE_ROOT / key)
        ts_print(f"[pre_ci] mypy [{label}]: cache dir "
                 f"{env['MYPY_CACHE_DIR']}")
        return env, True

    # Adapter-edited tests/examples files are type-checked individually:
    # full-repo tests/ mypy fails on mock-pattern noise in THIS runner's
    # environment (~336 errors on the cann runner; CI's lint image is
    # clean), but checking only vllm_ascend let adapter-edited
    # tests/e2e/conftest.py ship a type error that PR CI mypy caught
    # (PR #14135).  Diff against the adaptation base so both committed
    # step changes and uncommitted gate-fix edits are covered.  ``targets``
    # overrides the set entirely (gate-only full tests/examples audit).
    if targets is None:
        changed_extra = _changed_test_py_files(repo)
        if changed_extra:
            ts_print(f"[pre_ci] mypy: also checking {len(changed_extra)} "
                     f"changed tests/examples file(s)")
        check_targets = ["vllm_ascend", *changed_extra]
    else:
        check_targets = list(targets)

    all_violations: list[str] = []
    all_output: list[str] = []
    any_failed = False
    failed_units: list[str] = []
    matched: list[str] = []

    # Type-check against each vllm tree: the pinned main checkout plus,
    # when available, the release-tag worktree.  The release pass catches
    # call-site signature drift that the main-only pass cannot see (a
    # required release-only dataclass field reads as a normal keyword on
    # main).  No "diff looks version-sensitive" heuristic: the
    # max_seq_len_np class (PR #16296) is a plain call-site edit with no
    # vllm_version_is string anywhere near it.
    main_env, main_incremental = _cache_env(base_env, vllm_path or None,
                                            "main")
    main_vers = python_versions or ("3.10", "3.11", "3.12")
    trees: list[tuple[str, dict[str, str], tuple[str, ...], bool]] = [
        ("main", main_env, main_vers, main_incremental)]
    if vllm_release_path:
        release_abs = str(Path(vllm_release_path).resolve())
        release_env = base_env.copy()
        # Replace (not append) the vllm source in PYTHONPATH so mypy
        # resolves symbols against the release tree only.
        existing = base_env.get("PYTHONPATH", "")
        if vllm_path:
            vllm_abs = str(Path(vllm_path).resolve())
            existing = existing.replace(vllm_abs, "").strip(":")
        release_env["PYTHONPATH"] = (f"{release_abs}:{existing}"
                                     if existing else release_abs)
        release_label = f"release({Path(vllm_release_path).name})"
        release_env, release_incremental = _cache_env(
            release_env, vllm_release_path, release_label)
        trees.append((release_label, release_env, ("3.10",),
                      release_incremental))
        ts_print(f"[pre_ci] mypy: also checking against release tree: "
                 f"{release_abs}")

    for tree_label, tree_env, py_vers, incremental_ok in trees:
        for py_ver in py_vers:
            ts_print(f"[pre_ci] === mypy [{tree_label}] --python-version "
                     f"{py_ver} output begin ===")
            cmd = [*mypy_cmd, "--follow-imports", "skip",
                   "--check-untyped-defs", "--python-version", py_ver,
                   "--exclude", "_cann_ops_custom/"]
            if not incremental_ok:
                cmd.append("--no-incremental")
            r = subprocess.run(
                [*cmd, *check_targets],
                cwd=str(repo), capture_output=True, text=True, env=tree_env,
            )
            output = r.stdout + "\n" + r.stderr
            ts_print(output.strip())
            ts_print(f"[pre_ci] === mypy [{tree_label}] output end "
                     f"(py={py_ver}, exit={r.returncode}) ===")
            all_output.append(f"--- [{tree_label}] python {py_ver} "
                              f"(exit={r.returncode}) ---\n{output}")
            if filter_sigs is not None:
                # Whole-tree passes are expected to exit non-zero on this
                # runner (mock-pattern noise); only kept lines count.
                kept = [ln.strip() for ln in output.splitlines()
                        if ln.strip() and any(s in ln for s in filter_sigs)]
                matched.extend(kept)
                hit = bool(kept)
                if not kept and r.returncode not in (0, 1):
                    # mypy 0 = clean, 1 = type errors (expected noise here),
                    # 2+ = fatal (unreadable config, crash).  Never record a
                    # fatal as clean just because no line matched.
                    matched.append(output.strip()[-2000:]
                                   or f"mypy exited {r.returncode}")
                    hit = True
            else:
                hit = r.returncode != 0
            if hit:
                any_failed = True
                failed_units.append(f"{tree_label}/{py_ver}")

    if not any_failed:
        trees_ran = "main + release" if vllm_release_path else "main"
        vers = "/".join(main_vers)
        ts_print("\n[pre_ci] mypy: OK (all python versions clean on all trees)")
        return {"violations": [],
                "detail": f"mypy clean ({vers}, {trees_ran})"}

    if filter_sigs is not None:
        uniq: list[str] = []
        seen_filtered: set[str] = set()
        for line in matched:
            if line not in seen_filtered:
                seen_filtered.add(line)
                uniq.append(line)
        ts_print(f"\n[pre_ci] mypy: {len(uniq)} filtered issue(s) "
                 f"({', '.join(failed_units)})")
        for v in uniq[:20]:
            ts_print(f"  {v}")
        if len(uniq) > 20:
            ts_print(f"  ... and {len(uniq) - 20} more")
        return {"violations": uniq,
                "detail": f"{len(uniq)} mypy issue(s) "
                          f"({', '.join(failed_units)})"}

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
                "detail": f"{len(all_violations)} mypy issue(s) "
                          f"({', '.join(failed_units)})"}
    ts_print("\n[pre_ci] mypy: FAILED but no parseable error lines")
    return {"violations": ["\n".join(all_output)[-2000:]],
            "detail": "mypy failed but no parseable errors "
                      f"({', '.join(failed_units)})"}



def _module_binding_names(src_file: Path) -> set[str]:
    """Top-level names a module defines/exports (AST-based, best-effort).

    Covers defs/classes/assignments/imports, including inside top-level
    ``if`` blocks (upstream defines symbols under version guards too —
    conservative: any branch defines the name, we count it).
    """
    if not src_file.exists():
        return set()
    try:
        tree = ast.parse(src_file.read_text(encoding="utf-8"))
    except Exception:
        return set()
    names: set[str] = set()

    def _collect(node: ast.AST) -> None:
        for sub in ast.iter_child_nodes(node):
            if isinstance(sub, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
                names.add(sub.name)
            elif isinstance(sub, ast.Assign):
                for t in sub.targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
            elif isinstance(sub, ast.AnnAssign) and isinstance(sub.target, ast.Name):
                names.add(sub.target.id)
            elif isinstance(sub, ast.Import):
                for a in sub.names:
                    names.add(a.asname or a.name.split(".")[0])
            elif isinstance(sub, ast.ImportFrom):
                for a in sub.names:
                    names.add(a.asname or a.name)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    names.add(t.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                names.add(a.asname or a.name)
        elif isinstance(node, ast.If):
            # upstream modules define names under top-level version guards
            for sub in node.body:
                _collect(sub)
    return names


def _check_broken_imports(repo: Path, vllm_path: str | Path,
                          vllm_release_path: str | Path | None = None) -> dict:
    """Verify newly-added ``from vllm.X`` imports.

    1. Module must exist in the vllm tree (file or package dir).
    2. If the import is inside a ``vllm_version_is`` guard block, the line
       MUST carry ``# type: ignore[import-not-found]`` — mypy checks all
       static paths regardless of runtime guards.  No mypy needed here;
       this is a pure static check on the source text.
    3. If ``vllm_release_path`` (the pinned release worktree, e.g. v0.28.0)
       is given, every imported SYMBOL must also exist there — an unguarded
       module-level import of a main-only symbol (BatchReqState,
       cp_local_slot) crashes the whole release lane at import time
       (PRs #14517/#14580).  CI's pre-commit mypy resolves against the main
       tree only, so this is the only check that can see the class.
    """
    vllm_src = Path(vllm_path) / "vllm"
    release_src = Path(vllm_release_path) / "vllm" if vllm_release_path else None
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

        # Symbol-level check against the pinned release tree: an unguarded
        # module-level import of a main-only symbol crashes the whole lane
        # at import time (BatchReqState in #14517, cp_local_slot in #14580).
        if release_src is not None and not has_ignore and \
                int(entry["line_no"]) not in _guarded_lines(entry["file"]):
            if "import" in parts:
                idx = parts.index("import")
                symbols = [p.strip(",") for p in parts[idx + 1:]
                           if p.strip(",") and p.strip(",") != "*"]
                rel_mod_file = release_src / (mod.replace(".", "/") + ".py")
                rel_pkg_init = release_src / mod.replace(".", "/") / "__init__.py"
                release_names = (_module_binding_names(rel_mod_file)
                                 | _module_binding_names(rel_pkg_init))
                for sym in symbols:
                    if sym not in release_names:
                        violations.append(
                            f"{entry['file']}:{entry['line_no']}: symbol '{sym}' "
                            f"not found in the pinned release vllm tree — "
                            f"unguarded import crashes it at import time ({line})")

    return {"violations": violations}


_NEW_DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+(\w+)")
_NEW_SELF_STATE_RE = re.compile(r"^\s*self\.(\w+)\s*(?::[^=]+)?=(?!=)")
_CAPABILITY_PROBE_RE = re.compile(
    r"\b(?:importlib\.util\.)?find_spec\s*\(|\bhasattr\s*\(")
# `a, b, c = <expr>[:3]` — unpacking a fixed-length slice swallows a field
# that upstream main added (PR #20156's `draft_split[:3]`).
_ARITY_SLICE_RE = re.compile(
    r"^\s*[\w\s,.\[\]]+,[\w\s,.\[\]]+=\s*[\w.\[\]()'\" ]+\[\s*:\s*\d+\s*\]\s*$")


def _check_maintainability(added_lines: list[dict[str, str]]) -> dict:
    """ADVISORY shape check for mainline continuity (never blocks).

    The mechanical half of the "guard first, don't abstract" policy: the
    mechanical checks cannot decide whether a helper is justified, but the
    REJECTED shapes are syntactically visible.  Per file, the added lines
    are flagged when

    - a new ``def`` / new ``self.x =`` appears and the same file's added
      lines contain no ``vllm_version_is(...)`` (a compat-only helper or
      mirrored upstream state — PR #20156's ``_add_cached_input_id`` and
      ``request_hash_refs``), or
    - a capability probe (``find_spec`` / ``hasattr``) is used as a version
      boundary, or
    - a tuple unpack takes a fixed slice (``a, b, c = f(x)[:3]``), which
      hides an added upstream field from mypy and the release lane.

    Returns ``{"violations": [], "advisories": [...], "detail": str}``.
    ``violations`` is always empty by design: this check reports, it does
    not fail the step — a functionally correct adaptation must not be
    blocked by shape (adapter-qa and the gate review own the verdict).
    """
    by_file: dict[str, list[dict[str, str]]] = {}
    for entry in added_lines:
        by_file.setdefault(entry["file"], []).append(entry)

    advisories: list[dict[str, str]] = []
    for file_name, entries in sorted(by_file.items()):
        if not file_name.endswith(".py"):
            continue
        guarded = any("vllm_version_is(" in e["text"] for e in entries)
        for entry in entries:
            text = entry["text"]
            if text.lstrip().startswith("#"):
                continue
            rule = hint = ""
            if _CAPABILITY_PROBE_RE.search(text):
                rule = "capability_probe"
                hint = ("find_spec/hasattr is not a version check — guard the "
                        "contract with vllm_version_is at the call site")
            elif _ARITY_SLICE_RE.match(text):
                rule = "arity_slice"
                hint = ("unpack per version branch instead of slicing a "
                        "tuple whose arity differs across vllm versions")
            elif not guarded:
                m_def = _NEW_DEF_RE.match(text)
                m_state = _NEW_SELF_STATE_RE.match(text)
                if m_def:
                    rule = "new_def"
                    hint = (f"new definition '{m_def.group(1)}' with no version "
                            f"guard in this file — can it be one guarded "
                            f"branch instead of a compat helper?")
                elif m_state:
                    rule = "new_state"
                    hint = (f"new state attribute '{m_state.group(1)}' with no "
                            f"version guard in this file — upstream-main "
                            f"bookkeeping must not be mirrored onto both trees")
            if not rule:
                continue
            advisories.append({
                "file": file_name,
                "line": entry["line_no"],
                "rule": rule,
                "text": text.strip()[:160],
                "hint": hint,
            })

    if not advisories:
        detail = "no maintainability smells in added lines"
    else:
        detail = (f"{len(advisories)} maintainability advisory(ies) — "
                  f"non-blocking; see the adapter's guard-first rules")
        for adv in advisories[:12]:
            ts_print(f"[pre_ci][maintainability] {adv['file']}:{adv['line']}: "
                     f"{adv['rule']} — {adv['hint']}")
    return {"violations": [], "advisories": advisories, "detail": detail}


def run_check(ascend_path: str | Path, release_tag: str,
              vllm_path: str | Path | None = None,
              vllm_release_path: str | Path | None = None) -> dict:
    """Run pre-CI checks on the vllm-ascend working tree.

    Returns a dict with 'all_passed' (bool) and 'checks' (list of check results).
    If `vllm_path` is provided, also verifies that any new ``from vllm.X``
    imports in changed Python files reference modules that actually exist,
    and runs the mypy + CPU-UT gates (main vllm version) so type and
    unit-test regressions are caught at every step instead of only at the
    final quality gate.  If `vllm_release_path` is also provided, mypy runs
    a second pass against the release tree and imported SYMBOLS must exist
    there too (unguarded main-only imports crash the release lane).
    """
    repo = Path(ascend_path)

    try:
        added_lines = _get_added_lines(repo)
        versions = _check_version_strings(added_lines, release_tag)
        temps = _check_temp_files(repo)
        imports = (_check_broken_imports(repo, vllm_path, vllm_release_path)
                   if vllm_path else {"violations": []})
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

    fmt = _check_format(repo)
    fmt_ok = len(fmt["violations"]) == 0 or fmt.get("skipped", False)
    checks.append({
        "name": "format",
        "passed": fmt_ok,
        "detail": fmt["detail"],
        "violations": fmt["violations"],
        "skipped": fmt.get("skipped", False),
    })
    if not fmt_ok:
        all_passed = False

    # Maintainability (mainline-continuity) ADVISORIES — passed is always
    # True; the point is to surface the rejected shapes in
    # pre_ci_check.json / the job log while adapter-qa and the gate review
    # decide whether they block.
    maint = _check_maintainability(added_lines)
    checks.append({
        "name": "maintainability",
        "passed": True,
        "blocking": False,
        "detail": maint["detail"],
        "violations": [],
        "advisories": maint["advisories"],
    })

    if vllm_path:
        # mypy + CPU-UT gates, main vllm version (+ release tree for mypy
        # when vllm_release_path is set — the release pass runs inside the
        # mypy worker, keeping the 2-worker pool's peak resources unchanged).
        # Both self-skip (skipped=True) when the tool/vllm source is
        # unavailable, and a skipped check never fails the step.  Both are
        # read-only on the working tree and build separate venvs, so run
        # them CONCURRENTLY: the pre_ci wall clock becomes max(mypy, ut)
        # instead of the sum.  _check_format stays sequential above — its
        # auto-fix hooks mutate files and must finish before either reads
        # the tree.
        with ThreadPoolExecutor(max_workers=2) as pool:
            fut_mypy = pool.submit(_check_mypy, repo, vllm_path,
                                   vllm_release_path)
            fut_ut = pool.submit(_check_ut, repo, vllm_path)
            mypy = fut_mypy.result()
            ut = fut_ut.result()
        mypy_ok = len(mypy["violations"]) == 0 or mypy.get("skipped", False)
        checks.append({
            "name": "mypy",
            "passed": mypy_ok,
            "detail": mypy.get("detail", ""),
            "violations": mypy.get("violations", []),
            "skipped": mypy.get("skipped", False),
        })
        if not mypy_ok:
            all_passed = False

        ut_ok = len(ut["violations"]) == 0 or ut.get("skipped", False)
        checks.append({
            "name": "ut",
            "passed": ut_ok,
            "detail": ut.get("detail", ""),
            "violations": ut.get("violations", []),
            "skipped": ut.get("skipped", False),
            # Full-log path + persistent venv python — the fix-mode adapter
            # greps the log for complete tracebacks (in-session test runs
            # are guard-blocked; pre_ci stays the sole executor).
            "log_path": ut.get("log_path", ""),
            "venv_python": ut.get("venv_python", ""),
        })
        if not ut_ok:
            all_passed = False

    if not vllm_release_path:
        # Record WHY release-lane validation was absent (worktree setup
        # failed or MAIN2MAIN_RELEASE_GATE=0) so pre_ci_check.json is
        # self-explanatory.  Never fails the step.
        checks.append({
            "name": "release_lane",
            "passed": True,
            "detail": "release worktree unavailable — release-lane checks "
                      "skipped (mypy release pass + symbol imports)",
            "violations": [],
            "skipped": True,
        })

    return {"all_passed": all_passed, "checks": checks}
