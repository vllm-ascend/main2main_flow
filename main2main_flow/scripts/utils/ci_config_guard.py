"""CI-config write guard: the adapter may touch exactly two .github files.

Run 35513059821: the gate's static adapter-fix rounds "fixed" failing UT by
editing .github/workflows/_e2e_nightly_single_node_560t.yaml.  The repo's
own history teaches the pattern (HEAD was "[CI] Remove singlecard_ops test
entries from nightly config"), the adapter imitated it, every downstream
check was content-blind, and GitHub rejected the push 6/6 times (the
dev-submitter PAT lacks the workflow scope) — with the scope granted it
would have shipped a silent test-deselection instead.

Policy (user decision 2026-09-21, whitelist): under .github/ the adapter's
changes to EVERYTHING are reverted except the two pointer files the flow
itself must update and ship:

    .github/vllm-main-verified.commit
    .github/vllm-release-tag.commit

This is deliberately broader than GitHub's own PAT workflow-scope check
(which covers only .github/workflows/): composite actions under
.github/actions/ are executed code a workflow can call — editing them can
skew test behavior AND sails past the scope check, so they are guarded too,
as is every labeler/dependabot/template file.

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
# The only .github paths an adaptation may ever change: flow-owned pointer
# files that must ride along with the PR.
ALLOWED_PATHS = frozenset({
    ".github/vllm-main-verified.commit",
    ".github/vllm-release-tag.commit",
})


def _guarded(rel: str) -> bool:
    return rel.startswith(f"{GITHUB_DIR}/") and rel not in ALLOWED_PATHS


def snapshot(ascend_path: str | Path) -> dict[str, bytes]:
    """Byte-snapshot every guarded file under .github/ (tracked or not)."""
    root = Path(ascend_path) / GITHUB_DIR
    snap: dict[str, bytes] = {}
    if not root.is_dir():
        return snap
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(ascend_path))
        if _guarded(rel):
            snap[rel] = path.read_bytes()
    return snap


def restore(ascend_path: str | Path, snap: dict[str, bytes],
            phase: str) -> list[str]:
    """Undo every guarded .github/ change made since *snapshot*.

    Changed/deleted files are rewritten from the snapshot, files that
    appeared are removed, and the index entries of everything touched are
    dropped (adapter sessions stage with intent-to-add / git add).  Pointer
    files in ALLOWED_PATHS are never touched.  Returns the touched
    repo-relative paths (empty = clean session).
    """
    root = Path(ascend_path)
    gh_root = root / GITHUB_DIR
    current: dict[str, bytes] = {}
    if gh_root.is_dir():
        for path in sorted(gh_root.rglob("*")):
            if not path.is_file():
                continue
            rel = str(path.relative_to(root))
            if _guarded(rel):
                current[rel] = path.read_bytes()

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
                 f".github edit(s) ({shown}) — only vllm-main-verified.commit "
                 f"and vllm-release-tag.commit may change; fix the task in "
                 f"code instead")
    return sorted(touched)


def strip(ascend_path: str | Path, phase: str) -> list[str]:
    """Revert every guarded working-tree .github/ change vs HEAD.

    The squash-chokepoint backstop: staged edits, unstaged edits and
    untracked files under .github/ (minus the allowed pointer files) are
    all removed so the following ``git add -A`` + commit cannot pick them
    up.
    """
    changed = [p for p in
               run_git(ascend_path, "diff", "--name-only", "HEAD", "--",
                       GITHUB_DIR).split() if p]
    untracked = [p for p in
                 run_git(ascend_path, "ls-files", "--others",
                         "--exclude-standard", "--",
                         GITHUB_DIR).split() if p]
    paths = sorted(p for p in set(changed) | set(untracked) if _guarded(p))
    if not paths:
        return []
    in_head = {p for p in
               run_git(ascend_path, "ls-tree", "-r", "--name-only", "HEAD",
                       "--", GITHUB_DIR).split() if p}
    tracked = [p for p in paths if p in in_head]
    if tracked:
        run_git(ascend_path, "checkout", "HEAD", "--", *tracked)
    for p in paths:
        if p not in in_head:
            (Path(ascend_path) / p).unlink(missing_ok=True)
    ts_print(f"[ci-guard] {phase}: stripped {len(paths)} forbidden "
             f".github change(s) ({', '.join(paths[:5])}"
             f"{' …' if len(paths) > 5 else ''}) before committing — only "
             f"vllm-main-verified.commit and vllm-release-tag.commit may "
             f"change")
    return paths


def committed_violations(ascend_path: str | Path, base_ref: str) -> list[str]:
    """Guarded .github paths changed by commits above *base_ref*."""
    changed = [p for p in
               run_git(ascend_path, "diff", "--name-only",
                       f"{base_ref}..HEAD", "--", GITHUB_DIR).split() if p]
    return sorted(p for p in changed if _guarded(p))
