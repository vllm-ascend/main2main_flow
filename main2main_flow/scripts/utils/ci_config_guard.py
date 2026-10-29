"""CI-config write guard: the adapter may touch exactly two .github files.

Run 35513059821: the gate's static adapter-fix rounds "fixed" failing UT by
editing .github/workflows/_e2e_nightly_single_node_560t.yaml.  The repo's
own history teaches the pattern (HEAD was "[CI] Remove singlecard_ops test
entries from nightly config"), the adapter imitated it, every downstream
check was content-blind, and GitHub rejected the push 6/6 times (the
dev-submitter PAT lacks the workflow scope) — with the scope granted it
would have shipped a silent test-deselection instead.

Second evidence, PR #20156: the adapter edited .pre-commit-config.yaml to
pass ``args: ["-shellcheck=", "-pyflakes="]`` to actionlint, disabling two
lint subprocesses that hang inside this flow's container.  A hang is an
environment problem the flow itself suppresses (``SKIP``); committed as an
upstream config change it permanently weakens the repository's own linting
for every contributor.  Tooling config is therefore guarded too.

Policy (user decisions 2026-09-21 and 2026-10-29, whitelist):

- every change under GUARDED_DIRS is reverted, except the two pointer files
  the flow itself must update and ship::

    .github/vllm-main-verified.commit
    .github/vllm-release-tag.commit

- every change to a file in GUARDED_FILES (the repository's own lint /
  test tooling configuration) is reverted.

For .github this is deliberately broader than GitHub's own PAT
workflow-scope check (which covers only .github/workflows/): composite
actions under .github/actions/ are executed code a workflow can call —
editing them can skew test behavior AND sails past the scope check, so they
are guarded too, as is every labeler/dependabot/template file.

Three mechanical layers, none of them trusting the adapter's obedience:

1. ``snapshot``/``restore`` bracket every opencode adapter session, so a
   forbidden edit never survives long enough to be VERIFIED green by
   format/mypy/UT — a reverted cheat fails honestly on the next static run
   instead of sailing through as a false pass;
2. ``strip`` runs at both squash chokepoints (generate_final_post and
   push_to_github._force_squash) so no pushed commit can carry one;
3. push preflight aborts loudly if a committed forbidden change somehow
   still exists above the PR base.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from main2main_flow.scripts.utils.utils import run_git, ts_print

GITHUB_DIR = ".github"
# Directories whose entire content is guarded (minus ALLOWED_PATHS).
GUARDED_DIRS = (".github",)
# Individual tooling-config files guarded in full.  These are the
# repository's own lint/test configuration: "fixing" a check by editing them
# changes what upstream CI verifies for every contributor, not just this PR.
GUARDED_FILES = (".pre-commit-config.yaml",)
# The only .github paths an adaptation may ever change: flow-owned pointer
# files that must ride along with the PR.
ALLOWED_PATHS = frozenset({
    ".github/vllm-main-verified.commit",
    ".github/vllm-release-tag.commit",
})
# Where git is told to look — the guard scope as pathspecs.
GUARDED_PATHSPECS = (".github", ".pre-commit-config.yaml")


def _norm(rel: str) -> str:
    rel = rel.replace("\\", "/")
    return rel[2:] if rel.startswith("./") else rel


def _guarded(rel: str) -> bool:
    rel = _norm(rel)
    if rel in ALLOWED_PATHS:
        return False
    if rel in GUARDED_FILES:
        return True
    return any(rel == d or rel.startswith(f"{d}/") for d in GUARDED_DIRS)


def _guarded_snapshot(ascend_path: str | Path) -> dict[str, bytes]:
    """Read every guarded file that exists under *ascend_path*."""
    root = Path(ascend_path)
    snap: dict[str, bytes] = {}
    for dir_name in GUARDED_DIRS:
        top = root / dir_name
        if not top.is_dir():
            continue
        for path in sorted(top.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root).as_posix()
            if _guarded(rel):
                snap[rel] = path.read_bytes()
    for file_name in GUARDED_FILES:
        path = root / file_name
        if path.is_file() and _guarded(file_name):
            snap[file_name] = path.read_bytes()
    return snap


def snapshot(ascend_path: str | Path) -> dict[str, bytes]:
    """Byte-snapshot every guarded file (tracked or not)."""
    return _guarded_snapshot(ascend_path)


def restore(ascend_path: str | Path, snap: dict[str, bytes],
            phase: str) -> list[str]:
    """Undo every guarded change made since *snapshot*.

    Changed/deleted files are rewritten from the snapshot, files that
    appeared are removed, and the index entries of everything touched are
    dropped (adapter sessions stage with intent-to-add / git add).  Pointer
    files in ALLOWED_PATHS are never touched.  Returns the touched
    repo-relative paths (empty = clean session).
    """
    root = Path(ascend_path)
    current = _guarded_snapshot(root)

    touched: set[str] = set()
    for rel, old in snap.items():
        if current.get(rel) != old:
            # Modified or deleted by the session — rewrite from the
            # snapshot (also recreates a deleted file).
            touched.add(rel)
            (root / rel).write_bytes(old)
    for rel in current:
        if rel not in snap:
            touched.add(rel)
            (root / rel).unlink()
    # An empty dir the adapter just created is invisible to git; leave it.

    if touched:
        rels = sorted(touched)
        subprocess.run(["git", "reset", "-q", "--", *rels],
                       cwd=str(root), capture_output=True)
        shown = ", ".join(rels[:5]) + (" …" if len(rels) > 5 else "")
        ts_print(f"[ci-guard] {phase}: reverted {len(rels)} forbidden "
                 f"CI/tooling-config edit(s) ({shown}) — only "
                 f"vllm-main-verified.commit and vllm-release-tag.commit may "
                 f"change, and .pre-commit-config.yaml / .github/** are never "
                 f"an adaptation surface; fix the task in code instead")
    return sorted(touched)


def strip(ascend_path: str | Path, phase: str) -> list[str]:
    """Revert every guarded working-tree change vs HEAD.

    The squash-chokepoint backstop: staged edits, unstaged edits and
    untracked files in the guard scope (minus the allowed pointer files) are
    all removed so the following ``git add -A`` + commit cannot pick them
    up.
    """
    changed = [p for p in
               run_git(ascend_path, "diff", "--name-only", "HEAD", "--",
                       *GUARDED_PATHSPECS).split() if p]
    untracked = [p for p in
                 run_git(ascend_path, "ls-files", "--others",
                         "--exclude-standard", "--",
                         *GUARDED_PATHSPECS).split() if p]
    paths = sorted(p for p in set(changed) | set(untracked) if _guarded(p))
    if not paths:
        return []
    in_head = {p for p in
               run_git(ascend_path, "ls-tree", "-r", "--name-only", "HEAD",
                       "--", *GUARDED_PATHSPECS).split() if p}
    tracked = [p for p in paths if p in in_head]
    if tracked:
        run_git(ascend_path, "checkout", "HEAD", "--", *tracked)
    for p in paths:
        if p not in in_head:
            (Path(ascend_path) / p).unlink(missing_ok=True)
    ts_print(f"[ci-guard] {phase}: stripped {len(paths)} forbidden "
             f"CI/tooling-config change(s) ({', '.join(paths[:5])}"
             f"{' …' if len(paths) > 5 else ''}) before committing — only "
             f"vllm-main-verified.commit and vllm-release-tag.commit may "
             f"change, and .github/** / .pre-commit-config.yaml are never an "
             f"adaptation surface")
    return paths


def committed_violations(ascend_path: str | Path, base_ref: str) -> list[str]:
    """Guarded paths changed by commits above *base_ref*."""
    changed = [p for p in
               run_git(ascend_path, "diff", "--name-only",
                       f"{base_ref}..HEAD", "--",
                       *GUARDED_PATHSPECS).split() if p]
    return sorted(p for p in changed if _guarded(p))

