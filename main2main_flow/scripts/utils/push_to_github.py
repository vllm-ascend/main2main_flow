#!/usr/bin/env python3
"""Push the main2main patch as a new branch and open a GitHub pull request.

In CI mode (default when PUSH_TO_GITHUB=true):
  1. Ensure gh CLI is authenticated (use GH_TOKEN in CI, or existing gh auth).
  2. Configure git credential helper so git push uses the same token.
  3. If changes are already on a working branch (no --patch-path), use it directly;
     otherwise create a branch from the current commit and apply the final patch.
  4. Push the branch to the fork repo.
  5. Open a draft PR via ``gh pr create`` with proper commit-range title.
  6. Add labels to the PR.
  7. Write the PR URL to a file for downstream workflow steps.

In local mode (PUSH_TO_GITHUB not set):
  1-4: same as above.
  5. Open a regular (non-draft) PR.

Environment variables:
  PUSH_TO_GITHUB  — must be "true" to do anything
  GITHUB_REPO     — target repo "owner/name" (required, e.g. vllm-project/vllm-ascend)
  HEAD_FORK       — fork to push to (optional, e.g. vllm-ascend-ci/vllm-ascend)
  GH_TOKEN        — GitHub Personal Access Token (required in CI;
                    also used by git push via credential helper)
  PR_LABELS       — comma-separated labels to add (default: "ready")
  PR_DRAFT        — "true" (default) or "false"
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from main2main_flow.scripts.utils.utils import run_git, ts_print

DEFAULT_WORKSPACE_DIR = Path(__file__).parent.parent.parent / "workspace"
_PR_URL_FILE = "/tmp/main2main/pr_url.txt"


def _run_format(repo: Path) -> None:
    """Run format.sh to fix lint issues before commit."""
    fmt_script = repo / "format.sh"
    if not fmt_script.exists():
        return
    ts_print("[push] Running format.sh ...")
    before = subprocess.run(
        ["git", "diff", "--stat"], cwd=str(repo), capture_output=True, text=True,
    ).stdout.strip()
    ts_print("[push] === format.sh output begin ===")
    env = os.environ.copy()
    env["PRE_COMMIT_HOME"] = "/root/.cache/main2main-pre-commit"
    r = subprocess.run(
        ["bash", str(fmt_script)], cwd=str(repo), capture_output=True, text=True, env=env,
    )
    ts_print((r.stdout + "\n" + r.stderr).strip())
    ts_print(f"[push] === format.sh output end (exit={r.returncode}) ===")
    after = subprocess.run(
        ["git", "diff", "--stat"], cwd=str(repo), capture_output=True, text=True,
    ).stdout.strip()
    if after != before:
        ts_print(f"[push] format.sh fixed files (before → after commit)")
    else:
        ts_print("[push] format.sh: no files modified")


def _wait_for_fork_ref(head_fork: str, branch: str, expected_head: str,
                        timeout: int = 30) -> None:
    """Wait for the pushed branch to be visible on GitHub.

    After ``git push``, GitHub may take a moment to reflect the new ref.
    This polls ``git ls-remote`` until the fork branch matches the expected HEAD.
    """
    fork_url = f"https://github.com/{head_fork}.git"
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(
            ["git", "ls-remote", fork_url, f"refs/heads/{branch}"],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            remote_sha = r.stdout.strip().split()[0]
            if remote_sha == expected_head:
                ts_print(f"[push] Fork ref confirmed: {remote_sha[:8]}")
                return
        time.sleep(2)
    ts_print(f"[push] Warning: fork ref not confirmed within {timeout}s, proceeding anyway")


def _print_diff_diagnostics(ascend_path: Path, branch: str) -> None:
    """Print git diff and log for pre-push diagnostics."""
    r = subprocess.run(
        ["git", "diff", "--stat", "HEAD"],
        cwd=str(ascend_path), capture_output=True, text=True,
    )
    ts_print(f"[push] git diff --stat HEAD:\n{r.stdout.strip() or '(empty)'}")
    r = subprocess.run(
        ["git", "log", "--oneline", "-10"],
        cwd=str(ascend_path), capture_output=True, text=True,
    )
    ts_print(f"[push] git log --oneline -10:\n{r.stdout.strip()}")
    # Compare against upstream/main (vllm-project/vllm-ascend), which is the real base
    base_ref = _resolve_upstream_base(ascend_path, "main")
    if base_ref:
        r = subprocess.run(
            ["git", "rev-list", "--count", f"{base_ref}..{branch}"],
            cwd=str(ascend_path), capture_output=True, text=True,
        )
        count = r.stdout.strip()
        r2 = subprocess.run(
            ["git", "log", "--oneline", f"{base_ref}..{branch}"],
            cwd=str(ascend_path), capture_output=True, text=True,
        )
        ts_print(f"[push] Commits on {branch} not on upstream/main ({base_ref[:8]}): {count} commit(s)\n{r2.stdout.strip() or '(none)'}")
    else:
        ts_print("[push] Could not resolve upstream/main for comparison")


def _resolve_upstream_base(ascend_path: Path, base_branch: str) -> str:
    """Find the upstream base commit for comparison.

    Tries upstream/main, then origin/main, returns empty on failure.
    """
    for ref in (f"upstream/{base_branch}", f"origin/{base_branch}"):
        r = subprocess.run(
            ["git", "rev-parse", "--verify", ref],
            cwd=str(ascend_path), capture_output=True, text=True,
        )
        if r.returncode == 0:
            return r.stdout.strip()
    # Fallback: try ls-remote against the known upstream repo
    r = subprocess.run(
        ["git", "ls-remote", "https://github.com/vllm-project/vllm-ascend.git",
         f"refs/heads/{base_branch}"],
        capture_output=True, text=True,
    )
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip().split()[0]
    return ""


def _has_divergent_commits(ascend_path: Path, branch: str, base_sha: str) -> bool:
    """Check whether *branch* has commits that are not in *base_sha*."""
    r = subprocess.run(
        ["git", "rev-list", "--count", f"{base_sha}..{branch}"],
        cwd=str(ascend_path), capture_output=True, text=True,
    )
    count = int(r.stdout.strip()) if r.stdout.strip().isdigit() else 0
    return count > 0


def _detect_default_branch(repo: Path | str, remote: str = "origin") -> str:
    try:
        r = subprocess.run(
            ["git", "symbolic-ref", f"refs/remotes/{remote}/HEAD"],
            cwd=str(repo), capture_output=True, text=True, check=True,
        )
        return r.stdout.strip().rsplit("/", 1)[-1]
    except subprocess.CalledProcessError:
        return "main"


def _git_no_rewrite_prefix() -> list[str]:
    """Build a git command prefix that clears all url.*.insteadOf rewrites.

    Some CI runner images configure ``url.<proxy>.insteadOf = https://github.com/``
    to route GitHub through a domestic proxy.  This breaks ``git push`` because
    the credential helper (``gh auth git-credential``) is registered for
    github.com, not for the proxy host - git then prompts for username
    interactively and fails in non-interactive CI.  Enumerate every
    ``url.*.insteadof`` entry and pass ``-c <key>=`` to clear it for this
    single command.  ``gh`` CLI commands are unaffected (they use their own
    HTTP client), so only ``git push`` / ``git push --delete`` need this.
    """
    prefix = ["git"]
    r = subprocess.run(
        ["git", "config", "--get-regexp", r"^url\..*\.insteadof$"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return prefix
    for line in r.stdout.splitlines():
        parts = line.split(None, 1)
        if parts:
            prefix += ["-c", f"{parts[0]}="]  # empty value clears the rewrite
    return prefix


def _git_push(ascend_path: Path, branch: str) -> None:
    """Push branch to origin.

    Clears the extraheader that ``actions/checkout`` set (auto GITHUB_TOKEN,
    141 chars, scoped to upstream only) and replaces it with GH_TOKEN
    (= PAT_TOKEN, 40 chars classic PAT with fork write access).
    """
    token = os.environ.get("GH_TOKEN") or ""
    if not token:
        cmd = _git_no_rewrite_prefix() + ["push", "--force-with-lease", "origin", branch]
        run_git(ascend_path, *cmd[1:])
        return
    r = subprocess.run(
        _git_no_rewrite_prefix() + ["-c", "http.https://github.com/.extraheader=",
         "push", "--force-with-lease", "origin", branch],
        cwd=str(ascend_path), capture_output=True, text=True,
        env={**os.environ, "GITHUB_TOKEN": token},
    )
    if r.stdout.strip():
        ts_print(f"[push] git push stdout:\n{r.stdout.strip()}", flush=True)
    if r.returncode != 0:
        ts_print(
            f"[push] git push FAILED (exit {r.returncode}):\n"
            f"{r.stderr.strip() or '(no stderr)'}",
            flush=True,
        )
        r.check_returncode()


def _close_old_main2main_prs(github_repo: str, current_pr_number: str) -> None:
    """Close all open main2main auto-PRs except the current one.

    Identifies main2main PRs by title pattern ``[Misc]feat: adapt to vLLM main``
    and closes them with a comment pointing to the new PR.
    """
    r = subprocess.run(
        ["gh", "pr", "list", "--repo", github_repo,
         "--search", "[Misc]feat: adapt to vLLM main in:title",
         "--state", "open", "--json", "number,title",
         "--limit", "50"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        ts_print(f"[push] gh pr list failed: {r.stderr.strip()[:300]}")
        return
    try:
        prs = json.loads(r.stdout)
    except json.JSONDecodeError:
        ts_print(f"[push] gh pr list returned non-JSON: {r.stdout[:200]}")
        return
    ts_print(f"[push] Found {len(prs)} open main2main PR(s) to evaluate for closure")
    for pr in prs:
        num = str(pr.get("number", ""))
        if num == current_pr_number:
            continue
        title = pr.get("title", "")
        ts_print(f"[push] Closing old main2main PR #{num}: {title}")
        cr = subprocess.run(
            ["gh", "pr", "close", num, "--repo", github_repo,
             "-c", f"Superseded by #{current_pr_number}."],
            capture_output=True, text=True,
        )
        if cr.returncode != 0:
            ts_print(f"[push]   failed to close #{num}: {cr.stderr.strip()[:300]}")


def _update_baseline_ref(ascend_path: Path, head_fork: str,
                         source_branch: str) -> None:
    """Push the current vllm-ascend HEAD to refs/heads/main2main_baseline.

    The baseline ref marks "the vllm-ascend state corresponding to the last
    vllm commit that passed e2e".  Next day's run starts from this ref to
    do incremental adaptation instead of re-adapting from upstream/main.
    """
    if not head_fork:
        ts_print("[push] No HEAD_FORK configured, skipping baseline ref update")
        return
    fork_url = f"https://github.com/{head_fork}.git"
    r = subprocess.run(
        _git_no_rewrite_prefix() + ["push", fork_url,
         f"{source_branch}:refs/heads/main2main_baseline", "--force"],
        cwd=str(ascend_path), capture_output=True, text=True,
    )
    if r.returncode != 0:
        ts_print(f"[push] Warning: failed to update main2main_baseline: {r.stderr.strip()[:300]}")
    else:
        ts_print(f"[push] Updated main2main_baseline -> {source_branch}")


def _delete_old_main2main_branches(head_fork: str, keep_n: int = 3) -> None:
    """Delete old main2main_auto_* branches from the fork, keeping newest N."""
    if not head_fork:
        return
    # List all branches in the fork
    r = subprocess.run(
        ["gh", "api", f"repos/{head_fork}/branches", "--paginate",
         "-q", ".[].name"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        ts_print(f"[push] Warning: failed to list branches: {r.stderr.strip()[:200]}")
        return
    branches = [b.strip() for b in r.stdout.splitlines() if b.strip()]
    # Filter main2main_auto_* timestamped branches (NEVER touch main2main_baseline)
    auto_branches = sorted(
        [b for b in branches if b.startswith("main2main_auto_")],
        reverse=True,  # newest first by timestamp string
    )
    to_delete = auto_branches[keep_n:]
    if not to_delete:
        return
    ts_print(f"[push] Deleting {len(to_delete)} old main2main_auto_* branches (keeping newest {keep_n})")
    fork_url = f"https://github.com/{head_fork}.git"
    for b in to_delete:
        # Never delete baseline (defensive - shouldn't match the filter, but check anyway)
        if b == "main2main_baseline":
            continue
        dr = subprocess.run(
            _git_no_rewrite_prefix() + ["push", fork_url, "--delete", b],
            cwd=None, capture_output=True, text=True,
        )
        if dr.returncode != 0:
            ts_print(f"[push]   failed to delete {b}: {dr.stderr.strip()[:150]}")
        else:
            ts_print(f"[push]   deleted {b}")


def _add_labels(github_repo: str, pr_number: str, labels: list[str]) -> None:
    if not labels:
        return
    # Use REST API (not `gh pr edit` — that hits GraphQL which requires
    # read:org scope).  POST with a JSON array of label names.
    result = subprocess.run(
        ["gh", "api", "--method", "POST",
         "-H", "Accept: application/vnd.github+json",
         f"/repos/{github_repo}/issues/{pr_number}/labels"],
        input=json.dumps(labels),  # ["ready"]
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        ts_print(f"[push] Warning: Failed to add labels {labels}: {result.stderr.strip()}")
    else:
        ts_print(f"[push] Labels added: {labels}")


def push_and_create_pr(
    ascend_path: Path,
    github_repo: str,
    patch_path: Path | None = None,
    summary_path: Path | None = None,
    workspace_dir: Path = DEFAULT_WORKSPACE_DIR,
    old_commit: str = "",
    new_commit: str = "",
    head_fork: str = "",
    draft: bool = True,
    labels: list[str] | None = None,
    branch_name: str = "",
) -> str:
    """Create a branch (or reuse current), push to fork, and open a GitHub PR.

    Returns the PR URL, or "" when preconditions are not met.
    Raises subprocess.CalledProcessError on git/gh failure.
    """
    if not github_repo:
        ts_print("[push] GITHUB_REPO is empty, cannot create PR.", file=sys.stderr)
        return ""

    summary_file = summary_path or workspace_dir / "final_summary.md"
    if not summary_file.exists():
        ts_print(f"[push] Summary file not found: {summary_file}, using empty description.", file=sys.stderr)
        pr_description = ""
    else:
        pr_description = summary_file.read_text(encoding="utf-8")

    # ---- branch ----
    current_branch = run_git(ascend_path, "branch", "--show-current").strip()
    is_detached = not current_branch

    patch_file = patch_path.resolve() if patch_path else None
    has_patch = patch_file and patch_file.exists()

    if is_detached and not has_patch:
        ts_print("[push] Detached HEAD and no patch to apply, cannot push.", file=sys.stderr)
        return ""

    # Save current origin URL so we can restore it after push
    # Use `git config --get` (not `git remote get-url`) to read the RAW stored URL
    # without insteadOf rewrites — otherwise the saved URL becomes a ghfast.top URL
    # and `gh pr create` later can't recognize the GitHub host.
    _saved_origin_url = run_git(ascend_path, "config", "--get", "remote.origin.url").strip()

    try:
        # Decide branch and apply patch
        keep_branch = os.getenv("MAIN2MAIN_KEEP_BRANCH", "false").lower() == "true"
        if has_patch:
            if keep_branch and not is_detached:
                # Reuse existing branch, but still apply the cumulative patch
                # and commit — otherwise the push would send an empty branch.
                branch = current_branch
                ts_print(f"[push] Reusing branch '{branch}', committing working tree changes")
                _run_format(ascend_path)
                run_git(ascend_path, "add", "-A")
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                commit_msg = _build_commit_msg(old_commit, new_commit, ts)
                run_git(ascend_path, "commit", "-s", "-m", commit_msg)
                ts_print(f"[push] Committed as '{commit_msg}'.")
            else:
                # Create fresh branch and apply patch
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                branch = branch_name or f"update/main2main-{ts}"
                run_git(ascend_path, "checkout", "-b", branch)
                ts_print(f"[push] Created branch '{branch}', applying patch: {patch_file}")
                run_git(ascend_path, "apply", str(patch_file))
                _run_format(ascend_path)
                run_git(ascend_path, "add", "-A")
                commit_msg = _build_commit_msg(old_commit, new_commit, ts)
                run_git(ascend_path, "commit", "-s", "-m", commit_msg)
                ts_print(f"[push] Committed as '{commit_msg}'.")
        elif is_detached:
            branch = branch_name or f"update/main2main-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            run_git(ascend_path, "checkout", "-b", branch)
            ts_print(f"[push] Created branch '{branch}' from detached HEAD.")
        else:
            # Reuse current branch, no patch to apply
            branch = current_branch
            ts_print(f"[push] Reusing current branch '{branch}' (already has step commits).")

            # ---- diagnostics before push ----
        _print_diff_diagnostics(ascend_path, branch)
        if patch_file and patch_file.exists():
            content = patch_file.read_text(encoding="utf-8")
            ts_print(f"[push] final_target.patch ({len(content)} bytes):\n{content[:3000]}")
        else:
            ts_print("[push] No final_target.patch found, using branch commits directly.")

        # ---- push ----
        if head_fork:
            # Switch origin to the target fork repo (bypass mirror proxy for push),
            # push, then restore the original origin URL.
            fork_url = f"https://github.com/{head_fork}.git"
            run_git(ascend_path, "remote", "set-url", "origin", fork_url)
            ts_print(f"[push] Set origin to {fork_url}")
            _git_push(ascend_path, branch)
            head_ref = f"{head_fork.split('/')[0]}:{branch}"
            ts_print(f"[push] Pushed to {fork_url}")
            run_git(ascend_path, "remote", "set-url", "origin", _saved_origin_url)
        else:
            run_git(ascend_path, "push", "origin", branch)
            head_ref = branch
            ts_print(f"[push] Pushed branch '{branch}'.")

        # ---- PR ----
        base_branch = _detect_default_branch(ascend_path, remote="origin")
        local_head = run_git(ascend_path, "rev-parse", "HEAD").strip()
        ts_print(f"[push] Creating PR: head={head_ref} base={base_branch} repo={github_repo} local_head={local_head[:8]}")

        # Check if branch has commits that differ from the upstream base
        upstream_base = _resolve_upstream_base(ascend_path, base_branch)
        if upstream_base and not _has_divergent_commits(ascend_path, branch, upstream_base):
            ts_print(f"[push] Branch {branch} is at same commit as {base_branch} ({upstream_base[:8]}), skipping PR.")
            return ""

        # Verify the fork branch is visible on GitHub before PR creation
        if head_fork:
            _wait_for_fork_ref(head_fork, branch, local_head)

        pr_title = _build_pr_title(old_commit, new_commit)
        gh_cmd = [
            "gh", "pr", "create",
            "--title", pr_title,
            "--body", pr_description,
            "--head", head_ref,
            "--base", base_branch,
            "--repo", github_repo,
        ]
        if draft:
            gh_cmd.append("--draft")

        result = subprocess.run(
            gh_cmd, capture_output=True, text=True, cwd=str(ascend_path),
        )
        if result.returncode != 0:
            err = result.stderr.strip()
            ts_print(f"[push] PR create FAILED: {err}", flush=True)
            ts_print(f"[push] gh stdout: {result.stdout.strip()}", flush=True)
            if "No commits between" in err:
                ts_print("[push] No new commits to create PR for, skipping.")
                return ""
            result.check_returncode()
        pr_url = result.stdout.strip()
        ts_print(f"[push] PR created: {pr_url}")

        # ---- labels ----
        pr_number = pr_url.rstrip("/").rsplit("/", 1)[-1]
        if pr_number.isdigit():
            if labels is None:
                labels = ["ready"]
            _add_labels(github_repo, pr_number, labels)

        # ---- persist PR URL ----
        Path("/tmp/main2main").mkdir(parents=True, exist_ok=True)
        Path(_PR_URL_FILE).write_text(pr_url + "\n")
        ts_print(f"[push] PR URL written to {_PR_URL_FILE}")

        # ---- close old main2main PRs ----
        _close_old_main2main_prs(github_repo, pr_number)

        # ---- update baseline ref for next day's incremental run ----
        # The branch we just pushed contains the cumulative adaptation
        # state; mark it as the baseline so tomorrow's run can rebase on
        # top instead of starting from upstream/main.
        if branch_name:
            _update_baseline_ref(ascend_path, head_fork, branch_name)

        # ---- delete old main2main_auto_* timestamped branches ----
        keep_n = int(os.getenv("MAIN2MAIN_KEEP_BRANCHES", "3"))
        _delete_old_main2main_branches(head_fork, keep_n=keep_n)

    finally:
        # Only restore if we created a new branch from a different starting point
        if has_patch:
            run_git(ascend_path, "checkout", current_branch if not is_detached else "HEAD")
            ts_print(f"[push] Restored original ref.")

    return pr_url


def _build_commit_msg(old_commit: str, new_commit: str, ts: str) -> str:
    if old_commit and new_commit:
        short_old = old_commit[:8]
        short_new = new_commit[:8]
        return f"main2main: sync vllm upstream ({short_old}...{short_new}) [{ts}]"
    return f"main2main: sync vllm upstream ({ts})"


def _build_pr_title(old_commit: str, new_commit: str) -> str:
    if new_commit:
        return f"[Misc]feat: adapt to vLLM main ({new_commit[:8]})"
    return "main2main: sync vllm upstream"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Apply the main2main final patch to a new branch and open a GitHub PR."
    )
    parser.add_argument("--ascend-path", type=Path, required=True,
                        help="Local vllm-ascend repository path.")
    parser.add_argument("--patch-path", type=Path, default=None,
                        help="Path to final_target.patch (default: workspace/final_target.patch).")
    parser.add_argument("--summary-path", type=Path, default=None,
                        help="Markdown file used as PR description (default: workspace/final_summary.md).")
    parser.add_argument("--workspace-dir", type=Path, default=DEFAULT_WORKSPACE_DIR,
                        help="Workspace directory containing final_target.patch and final_summary.md.")
    parser.add_argument("--github-repo", default=os.getenv("GITHUB_REPO"),
                        required=not os.getenv("GITHUB_REPO"),
                        help="Target repo in owner/name form, e.g. vllm-project/vllm-ascend (or set $GITHUB_REPO).")
    parser.add_argument("--old-commit", default="",
                        help="Old vLLM commit for PR title (first 8 chars used).")
    parser.add_argument("--new-commit", default="",
                        help="New vLLM commit for PR title (first 8 chars used).")
    parser.add_argument("--head-fork", default=os.getenv("HEAD_FORK", ""),
                        help="Fork repo to push to, e.g. vllm-ascend-ci/vllm-ascend.")
    parser.add_argument("--draft", action="store_true",
                        default=os.getenv("PR_DRAFT", "true").lower() == "true",
                        help="Create as draft PR (default: true).")
    parser.add_argument("--labels", default=os.getenv("PR_LABELS", "ready"),
                        help="Comma-separated labels to add to the PR.")
    parser.add_argument("--branch-name", default="",
                        help="Branch name (auto-generated if empty).")
    parser.add_argument("--push", action="store_true",
                        default=os.getenv("PUSH_TO_GITHUB", "false").lower() == "true",
                        help="Actually push and create PR (default: $PUSH_TO_GITHUB).")
    args = parser.parse_args()

    if not args.push:
        ts_print("[push] PUSH_TO_GITHUB is not true, skipping.", file=sys.stderr)
        sys.exit(0)

    label_list = [lbl.strip() for lbl in args.labels.split(",") if lbl.strip()] if args.labels else []

    push_and_create_pr(
        ascend_path=args.ascend_path,
        patch_path=args.patch_path,
        summary_path=args.summary_path,
        workspace_dir=args.workspace_dir,
        github_repo=args.github_repo,
        old_commit=args.old_commit,
        new_commit=args.new_commit,
        head_fork=args.head_fork,
        draft=args.draft,
        labels=label_list,
        branch_name=args.branch_name,
    )


if __name__ == "__main__":
    main()
