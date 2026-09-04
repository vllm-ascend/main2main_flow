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
from main2main_flow.scripts.utils.ut_check import check_ut as _check_ut  # noqa: E402

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
        rr = run_format_sh(repo)
        out = _ANSI_RE.sub("", (rr.stdout + "\n" + rr.stderr))
        ts_print(out.strip() or "(no output)")
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
    violations = [re.sub(r'^#{0,2}\[error\]\s*|^::error::\s*', '', l)
                  for l in full.splitlines() if l.strip()]
    ts_print(f"\n[pre_ci] format: FAILED — {len(violations)} line(s) from "
             f"format.sh output (exit={rc2})")
    return {"violations": violations,
            "detail": f"format.sh FAILED (exit={rc2}) — residual lint errors "
                      f"after auto-fix pass; full output in violations"}


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
    # vllm-ascend's format.sh prints failing hook lines with an
    # "::error::" workflow-command prefix (rendered "##[error]" in the
    # runner log).  Without stripping it, EVERY lint violation from
    # format.sh was filtered here and the check reported OK while the
    # hooks failed — run 33784514899 shipped an E402 the upstream
    # pre-commit then caught (2026-09-04).
    s = re.sub(r'^#{1,2}\[error\]\s*|^::error::\s*', '', s)
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

    Single-version validation: mypy resolves symbols against the target
    main vllm tree only.

    Returns all errors mypy reports across all python versions - no
    added-line filtering.  CI mypy is the source of truth; if it fails,
    the adaptation must be fixed.
    """
    mypy = shutil.which("mypy")
    if not mypy:
        return {"violations": [], "detail": "mypy not installed", "skipped": True}

    import os as _os
    base_env = _os.environ.copy()
    if vllm_path:
        vllm_abs = str(Path(vllm_path).resolve())
        existing = base_env.get("PYTHONPATH", "")
        base_env["PYTHONPATH"] = f"{vllm_abs}:{existing}" if existing else vllm_abs
        ts_print(f"\n[pre_ci] mypy: PYTHONPATH includes vllm source: {vllm_abs}")

    # CI's lint image runs mypy in a clean environment: no vllm package
    # installed (mypy uses PYTHONPATH for vllm source), numpy 1.26.4
    # (constrained by triton-ascend's metadata, installed WITHOUT --no-deps).
    #
    # mypy runs on the SYSTEM mypy/numpy — the exact upstream model
    # (tools/mypy.sh runs on the bare runner; upstream pr_test cpu-0
    # installs nothing extra).  The lint-equivalent venv that pinned numpy
    # 1.26.4 was removed: its reason (system numpy 2.x causing ~68 spurious
    # errors) is gone once the environment matches upstream — verified
    # 2026-09-02: system numpy 1.26.4, clean mypy on the pristine tree.
    mypy_cmd = [mypy]

    # Clear mypy cache - it may have cached type info from numpy 2.x or
    # the installed vllm package (target commit).
    import shutil as _shutil
    cache = repo / ".mypy_cache"
    if cache.exists():
        _shutil.rmtree(cache, ignore_errors=True)
        ts_print("\n[pre_ci] mypy: cleared .mypy_cache")

    # Adapter-edited tests/examples files are type-checked individually:
    # full-repo tests/ mypy fails on mock-pattern noise in THIS runner's
    # environment (~336 errors on the cann runner; CI's lint image is
    # clean), but checking only vllm_ascend let adapter-edited
    # tests/e2e/conftest.py ship a type error that PR CI mypy caught
    # (PR #14135).  Diff against the adaptation base so both committed
    # step changes and uncommitted gate-fix edits are covered.
    changed_extra = _changed_test_py_files(repo)
    if changed_extra:
        ts_print(f"[pre_ci] mypy: also checking {len(changed_extra)} "
                 f"changed tests/examples file(s)")

    all_violations: list[str] = []
    all_output: list[str] = []
    any_failed = False

    # Single-version validation: mypy resolves symbols against the
    # target main vllm tree only.
    for py_ver in ("3.10", "3.11", "3.12"):
        ts_print(f"[pre_ci] === mypy [main] --python-version {py_ver} "
                 f"output begin ===")
        r = subprocess.run(
            [*mypy_cmd, "--follow-imports", "skip", "--check-untyped-defs",
             "--python-version", py_ver,
             "--exclude", "_cann_ops_custom/",
             "vllm_ascend", *changed_extra],
            cwd=str(repo), capture_output=True, text=True, env=base_env,
        )
        output = r.stdout + "\n" + r.stderr
        ts_print(output.strip())
        ts_print(f"[pre_ci] === mypy [main] output end "
                 f"(py={py_ver}, exit={r.returncode}) ===")
        all_output.append(f"--- [main] python {py_ver} "
                          f"(exit={r.returncode}) ---\n{output}")
        if r.returncode != 0:
            any_failed = True
    if not any_failed:
        ts_print("\n[pre_ci] mypy: OK (all python versions clean)")
        return {"violations": [], "detail": "mypy clean (3.10/3.11/3.12, main)"}

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
    imports in changed Python files reference modules that actually exist,
    and runs the mypy + CPU-UT gates (single main vllm version) so type and
    unit-test regressions are caught at every step instead of only at the
    final quality gate.
    """
    repo = Path(ascend_path)

    try:
        added_lines = _get_added_lines(repo)
        versions = _check_version_strings(added_lines, release_tag)
        temps = _check_temp_files(repo)
        imports = (_check_broken_imports(repo, vllm_path)
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

    # FULL format.sh (all pre-commit hooks — ruff, codespell, typos,
    # clang-format, markdownlint, actionlint), same as the final quality
    # gate and upstream's pre-commit job.  A per-step ruff-check-only pass
    # left codespell/typos findings invisible until the gate, and the
    # format run itself printed nothing (user-visible: no format activity
    # in step logs, 2026-09-02).  _check_format prints begin/output/end.
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

    if vllm_path:
        # mypy + CPU-UT gates, single main vllm version.  Both self-skip
        # (skipped=True) when the tool/vllm source is unavailable, and a
        # skipped check never fails the step.
        mypy = _check_mypy(repo, vllm_path)
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

        ut = _check_ut(repo, vllm_path)
        ut_ok = len(ut["violations"]) == 0 or ut.get("skipped", False)
        checks.append({
            "name": "ut",
            "passed": ut_ok,
            "detail": ut.get("detail", ""),
            "violations": ut.get("violations", []),
            "skipped": ut.get("skipped", False),
            # Full pytest outputs of the failing runs — flows into the
            # adapter's error_logs inline (head+tail kept on truncation),
            # so a collection error the violation regex misses is still
            # visible (33538038959 gate: ImportError hidden behind a
            # pytest-asyncio deprecation-warning tail for 3 fix rounds).
            "full_outputs": ut.get("full_outputs", {}),
        })
        if not ut_ok:
            all_passed = False

    return {"all_passed": all_passed, "checks": checks}
