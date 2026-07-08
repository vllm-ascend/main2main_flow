"""Pre-CI verification for main2main steps.

Runs mechanical checks before CI to catch common adaptation errors:
  1. Version string consistency: newly added vllm_version_is() calls use
     the correct release tag (scoped to current diff, not the whole repo).
  2. Temp file cleanliness: no intermediate files in the repository.
  3. Format: repo's format.sh runs clean (skipped when its tools are missing).
  4. Broken imports: `from vllm.X import ...` statements on ADDED lines
     resolve against the target vllm tree.

Design note:
    The version string and broken-imports checks only examine lines ADDED in
    the current diff (git diff HEAD), not the entire repo. Previous main2main
    runs leave behind guards like vllm_version_is("0.20.2") that are correct
    for that version boundary; likewise pre-existing imports were valid at an
    older vllm commit. Scanning the full repo would flag historical code
    whenever the release tag or target commit advances.

    The imports check is AST-based (not line regexes) so multi-line imports
    parse correctly, and it suppresses imports guarded by a
    vllm_version_is() branch or a try/except ImportError — those are the
    legitimate ways to reference an old vllm path. A file that fails
    ast.parse is itself a violation: a syntax error must fail pre-CI.
"""
from __future__ import annotations

import ast
import re
import subprocess
from pathlib import Path

from main2main_flow.utils import run_git, ts_print

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
    status_output = run_git(repo, "status", "--porcelain=v1", "-z")
    untracked_output = run_git(repo, "ls-files", "--others", "--exclude-standard")

    all_files: set[str] = set()
    # -z records: "XY <path>"; rename/copy records are followed by a second
    # NUL-separated origin path — the destination comes first, skip the origin.
    tokens = status_output.split("\0")
    i = 0
    while i < len(tokens):
        record = tokens[i]
        i += 1
        if len(record) < 4:
            continue
        status, filepath = record[:2], record[3:]
        all_files.add(filepath)
        if "R" in status or "C" in status:
            i += 1
    for line in untracked_output.splitlines():
        if line.strip():
            all_files.add(line.strip())

    violations: list[str] = []
    for filepath in sorted(all_files):
        basename = Path(filepath).name
        for pattern in _TEMP_PATTERNS:
            # extension-like patterns match only at the end; others as substring
            if (basename.endswith(pattern) if pattern.startswith(".")
                    else pattern in basename):
                violations.append(filepath)
                break

    return {"violations": violations}


def _check_format(repo: Path) -> dict:
    """Run ``bash format.sh`` to check ruff format/lint."""
    fmt_script = repo / "format.sh"
    if not fmt_script.exists():
        return {"violations": [], "detail": "format.sh not found"}
    r = subprocess.run(
        ["bash", str(fmt_script)], cwd=str(repo),
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        return {"violations": [], "detail": "format.sh OK"}
    output = (r.stdout + "\n" + r.stderr).strip()[:4000]
    if "command not found" in output or "No module named" in output:
        # infra problem, not the adaptation's fault — never ask the agent to fix it
        ts_print(f"[pre-ci] format.sh could not run (missing tools):\n{output}")
        return {"violations": [], "detail": "format.sh could not run (missing tools)"}
    return {"violations": [output], "detail": "format.sh failed"}


def _handles_import_error(handler: ast.ExceptHandler) -> bool:
    if handler.type is None:
        return False
    for node in ast.walk(handler.type):
        if isinstance(node, ast.Name) and node.id in (
                "ImportError", "ModuleNotFoundError"):
            return True
        if isinstance(node, ast.Attribute) and node.attr in (
                "ImportError", "ModuleNotFoundError"):
            return True
    return False


def _is_guarded(node: ast.AST, parents: dict[ast.AST, ast.AST],
                source: str) -> bool:
    """True when an ancestor guards the import: a vllm_version_is() branch
    or a try with an except ImportError/ModuleNotFoundError handler."""
    cur = parents.get(node)
    while cur is not None:
        if isinstance(cur, ast.If):
            test_src = ast.get_source_segment(source, cur.test) or ""
            if "vllm_version_is" in test_src:
                return True
        elif isinstance(cur, ast.Try):
            if any(_handles_import_error(h) for h in cur.handlers):
                return True
        cur = parents.get(cur)
    return False


def _check_broken_imports(repo: Path, vllm_path: Path,
                          added: list[dict[str, str]]) -> dict:
    """Verify ``from vllm.X`` imports on ADDED lines resolve in the vllm tree."""
    added_by_file: dict[str, set[int]] = {}
    for entry in added:
        if entry["file"].endswith(".py"):
            added_by_file.setdefault(entry["file"], set()).add(
                int(entry["line_no"]))

    violations: list[str] = []
    for fname, added_lines in sorted(added_by_file.items()):
        fp = repo / fname
        if not fp.exists():
            continue
        source = fp.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            violations.append(f"{fname}: unparseable file: {exc}")
            continue

        parents: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node

        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module
            # ``from vllm import X`` is symbol-level — not checkable here.
            if node.level or not module or not module.startswith("vllm."):
                continue
            if node.lineno not in added_lines:
                continue
            if _is_guarded(node, parents, source):
                continue
            rel = module.replace(".", "/")
            if ((vllm_path / f"{rel}.py").exists()
                    or (vllm_path / rel / "__init__.py").exists()):
                continue
            violations.append(
                f"{fname}:{node.lineno}: from {module} import ... "
                f"— module not found under {vllm_path}/")

    return {"violations": violations}


def run_check(ascend_path: str | Path, release_tag: str,
              vllm_path: str | Path | None = None) -> dict:
    """Run pre-CI checks on the vllm-ascend working tree.

    Returns a dict with 'all_passed' (bool) and 'checks' (list of check results).
    If `vllm_path` is provided, also verifies that ``from vllm.X`` imports on
    lines added in the current diff reference modules that actually exist.
    """
    repo = Path(ascend_path)

    try:
        added_lines = _get_added_lines(repo)
        versions = _check_version_strings(added_lines, release_tag)
        temps = _check_temp_files(repo)
        fmt = _check_format(repo)
        imports = (_check_broken_imports(repo, Path(vllm_path), added_lines)
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

    fmt_ok = len(fmt["violations"]) == 0
    checks.append({
        "name": "format",
        "passed": fmt_ok,
        "detail": fmt["detail"],
        "violations": fmt["violations"],
    })
    if not fmt_ok:
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
