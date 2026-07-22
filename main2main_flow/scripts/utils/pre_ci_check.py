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
from pathlib import Path

from main2main_flow.scripts.utils.utils import run_git, ts_print

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


def _get_added_lines(repo: Path) -> list[dict[str, str]]:
    diff_output = run_git(repo, "diff", "HEAD", "-U0")
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


def _check_mypy(repo: Path) -> dict:
    """Run mypy on changed Python files, only flagging errors on ADDED lines.

    Pre-existing mypy issues in the codebase are not the AI's fault —
    only errors on lines introduced by the current adaptation count.
    """
    py_files = _changed_py_files(repo)
    if not py_files:
        return {"violations": [], "detail": "no changed .py files"}
    mypy = shutil.which("mypy")
    if not mypy:
        return {"violations": [], "detail": "mypy not installed", "skipped": True}

    # Collect added line numbers per file from git diff
    added_lines = _get_added_lines(repo)
    added_locations: dict[str, set[int]] = {}
    for entry in added_lines:
        added_locations.setdefault(entry["file"], set()).add(int(entry["line_no"]))

    r = subprocess.run(
        [mypy, "--follow-imports", "skip",
         "--check-untyped-defs"] + py_files,
        cwd=str(repo), capture_output=True, text=True,
    )
    if r.returncode == 0:
        ts_print("[pre_ci] mypy: OK")
        return {"violations": [], "detail": "mypy clean"}

    # Parse mypy output: "file.py:LINE:COL: error:" or "file.py:LINE: error:"
    _MYPY_ERR_RE = re.compile(r"^(.+\.py):(\d+):(?:\d+:\s+)?error:")
    violations: list[str] = []
    for line in (r.stdout + "\n" + r.stderr).splitlines():
        m = _MYPY_ERR_RE.search(line.strip())
        if not m:
            continue
        fname = m.group(1)
        lineno = int(m.group(2))
        # Only flag if the error is on a line ADDED by this adaptation
        if lineno in added_locations.get(fname, set()):
            violations.append(line.strip())

    if violations:
        ts_print(f"[pre_ci] mypy: {len(violations)} issue(s) on added lines")
        return {"violations": violations,
                "detail": f"{len(violations)} mypy issue(s) on new code"}
    ts_print("[pre_ci] mypy: OK (pre-existing issues on unchanged lines ignored)")
    return {"violations": [], "detail": "mypy clean (new code only)"}


def _changed_py_files(repo: Path) -> list[str]:
    """Return list of changed .py files in the working tree (vs HEAD)."""
    r = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"], cwd=str(repo),
        capture_output=True, text=True,
    )
    return [f for f in r.stdout.strip().splitlines()
            if f.endswith(".py") and Path(repo, f).exists()]


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
        ts_print("[pre_ci] format: SKIPPED — format.sh not found")
        return {"violations": [], "detail": "format.sh not found", "skipped": True}
    if not shutil.which("pre-commit"):
        ts_print("[pre_ci] format: SKIPPED — pre-commit not installed, all lint checks bypassed!")
        return {"violations": [], "detail": "pre-commit not installed", "skipped": True}

    env = os.environ.copy()
    env["PRE_COMMIT_HOME"] = "/root/.cache/main2main-pre-commit"

    ts_print("[pre_ci] === format.sh output begin ===")
    r = subprocess.run(
        ["bash", str(fmt_script)], cwd=str(repo),
        capture_output=True, text=True, env=env,
    )
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
        ts_print(f"[pre_ci] format: {len(real_errors)} non-auto-fixable issue(s):")
        for e in real_errors[:20]:
            ts_print(f"  {e}")
        return {"violations": real_errors,
                "detail": f"{len(real_errors)} lint issue(s) (not auto-fixable)"}
    ts_print("[pre_ci] format: OK")
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
    return True


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
        fmt = _check_format(repo)
        imports = _check_broken_imports(repo, vllm_path) if vllm_path else {"violations": []}
        mypy = _check_mypy(repo)
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

    fmt_ok = len(fmt["violations"]) == 0
    checks.append({
        "name": "format",
        "passed": fmt_ok or fmt.get("skipped", False),
        "detail": fmt["detail"],
        "violations": fmt["violations"],
        "skipped": fmt.get("skipped", False),
    })
    if not fmt_ok and not fmt.get("skipped"):
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

    mypy_ok = len(mypy["violations"]) == 0
    checks.append({
        "name": "mypy",
        "passed": mypy_ok or mypy.get("skipped", False),
        "detail": mypy["detail"],
        "violations": mypy["violations"],
        "skipped": mypy.get("skipped", False),
    })
    if not mypy_ok and not mypy.get("skipped"):
        all_passed = False

    return {"all_passed": all_passed, "checks": checks}
