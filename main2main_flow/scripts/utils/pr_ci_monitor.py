"""Closed-loop watcher for a submitted main2main PR's upstream CI.

After ``push_and_create_pr`` returns, the PR's E2E workflow (pr_test.yaml)
is the only executor of the full dual-version matrix — the flow's own
gates never run the release-lane engine.  This module polls the PR's
check-runs, classifies failures, and repairs the PR:

- rebase conflicts / CSRC drift  -> deterministic rebase + lease push
- infra failures                 -> rerun the failed jobs (budgeted)
- content failures               -> own-diff triage; root cause inside the
  adaptation diff -> adapter-fix round, entirely outside it (the #16382
  upstream-inherited shape) -> evidence + PR comment only

The upstream CI stays the only test executor here — the monitor runs no
tests itself, mirroring the pre_ci-only-executor philosophy.  Fixes are
appended as commits and force-pushed with a fresh lease; on success the
fix is also pushed to ``main2main_baseline`` so the next daily run
inherits it (the daily bot rewrites the PR branch force-with-lease, which
would otherwise discard the fix).

Every failure of the watcher itself is recorded, never propagated: by the
time it runs, the flow run has already succeeded.
"""

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Callable

from main2main_flow.scripts.utils.push_to_github import (
    _push_with_lease,
    _update_baseline_ref,
)
from main2main_flow.scripts.utils.utils import (
    EACH_STEP_CODE_STRUCTURE_GUIDE_FILE,
    PR_CI_WATCH_DIR,
    PR_CI_WATCH_RESULT_FILE,
    run_git,
    ts_print,
)

UPSTREAM_URL = "https://github.com/vllm-project/vllm-ascend.git"
UPSTREAM_FETCH_REF = "refs/remotes/m2m-upstream/main"
PR_URL_FILE = "/tmp/main2main/pr_url.txt"
# A PR head with zero check-runs AND zero workflow runs after this grace
# period means no CI will ever run (local/dev push) — stop instead of
# burning the round timeout.
NO_CI_GRACE_SEC = 15 * 60

# Conclusions that do not block the PR.
_IGNORED_CONCLUSIONS = {"success", "neutral", "skipped"}

# ci-gate is the aggregator: it fails whenever any upstream job fails and
# its own log names no test.  Fixing the real job fixes it.
_AGGREGATOR_MARKERS = ("ci-gate",)

# Failure signatures inside a job log, ordered most-specific first.
_CSRC_SIG = "CSRC build workflows changed"
_REBASE_SIGS = ("CONFLICT (", "error: could not apply")
_CONTENT_SIGS = ("=== FAILURES", "=== ERRORS", "AssertionError",
                 "FAILED tests", "FAILED ", "ERROR tests",
                 "error during collection")

# Ascend-tree python paths for own-diff triage (CI logs carry the runner's
# absolute prefix; the regex matches the repo-relative suffix).  Only
# failure-attribution lines count — job logs also print the full
# selected-test listing, and a bare path there is not a root cause.
_PY_PATH = r"(?:vllm_ascend|tests)/[\w./-]+\.py"
_FRAME_RE = re.compile(rf'File "[^"]*?(?P<path>{_PY_PATH})"')
_MARK_RE = re.compile(rf"^(?:FAILED|ERROR)\s+(?P<path>{_PY_PATH})")
_MYPY_RE = re.compile(rf"^(?P<path>{_PY_PATH}):\d+:")

# The verbatim "MCP server is registered" marker keeps the adapter's
# vllm-report lessons pointer alive in fix mode (opencode_adapter).
_VLLM_REPORT_CTX = (
    "vllm-report MCP server is registered in opencode.jsonc. "
    "Call its tools dynamically (see \"vllm-report MCP Tools\" "
    "section below).  Call tool_get_adaptation_lessons to find "
    "prior lessons matching these failures before fixing."
)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _env_flag(name: str, default: str) -> bool:
    return os.getenv(name, default).lower() not in ("0", "false", "no")


def _gh_api(path: str) -> dict:
    """gh api wrapper; raises RuntimeError with the stderr tail on failure."""
    r = subprocess.run(["gh", "api", path], capture_output=True, text=True,
                       timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"gh api {path} failed: {r.stderr.strip()[-300:]}")
    return json.loads(r.stdout)


def _parse_pr_url(pr_url: str) -> tuple[str, str]:
    """'https://github.com/o/r/pull/12' -> ('o/r', '12')."""
    m = re.search(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)", pr_url)
    if not m:
        raise ValueError(f"cannot parse PR url: {pr_url}")
    return m.group(1), m.group(2)


def _pr_view(repo: str, num: str) -> dict:
    return _gh_api(f"repos/{repo}/pulls/{num}")


def _pr_closed(pr: dict) -> bool:
    return pr.get("state") == "closed" or bool(pr.get("merged"))


def _check_runs(repo: str, sha: str) -> list[dict]:
    data = _gh_api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100")
    return data.get("check_runs", [])


def _parse_details_url(url: str) -> tuple[str, str]:
    """.../actions/runs/<run_id>/job/<job_id> -> (run_id, job_id)."""
    m = re.search(r"/actions/runs/(\d+)/job/(\d+)", url or "")
    return (m.group(1), m.group(2)) if m else ("", "")


def _fetch_job_logs(repo: str, job_id: str, dest: Path) -> str:
    """Download one job's log into dest; returns the text (empty on error)."""
    r = subprocess.run(["gh", "api", f"repos/{repo}/actions/jobs/{job_id}/logs"],
                       capture_output=True, text=True, timeout=300)
    text = r.stdout if r.returncode == 0 else ""
    if r.returncode != 0:
        ts_print(f"[pr-ci-watch] job log fetch failed ({job_id}): "
                 f"{r.stderr.strip()[-200:]}")
    dest.write_text(text, encoding="utf-8")
    return text


def classify_log(text: str) -> str:
    """Signature-classify one failing job log: rebase|csrc|content|infra."""
    if _CSRC_SIG in text:
        return "csrc"
    if any(sig in text for sig in _REBASE_SIGS):
        return "rebase"
    if any(sig in text for sig in _CONTENT_SIGS):
        return "content"
    # Failed with no recognizable content signature: setup/runner/cache
    # classes, where a rerun is both the diagnosis and the fix.
    return "infra"


def extract_failure_files(text: str) -> list[str]:
    """Repo-relative .py paths of likely root causes inside a CI log."""
    seen: list[str] = []
    for line in text.splitlines():
        m = (_FRAME_RE.search(line) or _MARK_RE.search(line)
             or _MYPY_RE.search(line))
        if m and m.group("path") not in seen:
            seen.append(m.group("path"))
    return seen


def triage_own_diff(files: list[str], diff_files: list[str]) -> str:
    """'own' | 'inherited' — conservative to 'own' when nothing extracted."""
    if not files:
        return "own"
    if set(files) & set(diff_files):
        return "own"
    return "inherited"


def _latest_structure_guide(workspace_dir: Path) -> str:
    """Path of the most recent step's code-structure guide, if any."""
    steps = Path(workspace_dir) / "steps"
    if not steps.is_dir():
        return ""
    guides = sorted(steps.glob(f"*/{EACH_STEP_CODE_STRUCTURE_GUIDE_FILE}"))
    return str(guides[-1]) if guides else ""


def _workspace_vllm_path(workspace_dir: Path) -> str:
    vllm = Path(workspace_dir) / "repos" / "vllm"
    return str(vllm) if vllm.is_dir() else ""


def _ensure_branch(ascend_path: Path, branch: str, fork_url: str) -> bool:
    """Put the ascend checkout on the PR branch — every local action
    (adapter edits, commits, pushes) operates on this tree.

    In-flow the checkout is already on the branch (KEEP_BRANCH); standalone
    runs get a best-effort fetch + checkout -B from the fork.
    """
    current = run_git(ascend_path, "branch", "--show-current").strip()
    if current == branch:
        return True
    ts_print(f"[pr-ci-watch] checkout is on '{current}', fetching PR branch "
             f"'{branch}'")
    subprocess.run(["git", "fetch", "--force", fork_url,
                    f"refs/heads/{branch}:{UPSTREAM_FETCH_REF}"],
                   cwd=str(ascend_path), capture_output=True, text=True,
                   timeout=300)
    r = subprocess.run(["git", "checkout", "-B", branch, UPSTREAM_FETCH_REF],
                       cwd=str(ascend_path), capture_output=True, text=True)
    if r.returncode != 0:
        ts_print(f"[pr-ci-watch] checkout to PR branch failed: "
                 f"{r.stderr.strip()[-300:]}")
        return False
    return True


def _fetch_upstream_main(ascend_path: Path) -> bool:
    """Refresh refs/remotes/m2m-upstream/main from the upstream repo."""
    r = subprocess.run(["git", "fetch", "--force", UPSTREAM_URL,
                        "refs/heads/main:" + UPSTREAM_FETCH_REF],
                       cwd=str(ascend_path), capture_output=True, text=True,
                       timeout=300)
    if r.returncode != 0:
        ts_print(f"[pr-ci-watch] upstream main fetch failed: "
                 f"{r.stderr.strip()[-300:]}")
    return r.returncode == 0


def _run_gh(args: list[str]) -> bool:
    r = subprocess.run(["gh", *args], capture_output=True, text=True,
                       timeout=300)
    if r.returncode != 0:
        ts_print(f"[pr-ci-watch] gh {' '.join(args)} failed: "
                 f"{r.stderr.strip()[-300:]}")
    return r.returncode == 0


def _rerun_failed(failing: list[dict]) -> None:
    """Rerun the failed jobs of every distinct workflow run involved."""
    run_ids = {_parse_details_url(c.get("details_url", ""))[0]
               for c in failing} - {""}
    for run_id in sorted(run_ids):
        _run_gh(["run", "rerun", run_id, "--failed"])


def _push_fix(ascend_path: Path, branch: str, with_baseline: bool,
              message: str) -> bool:
    """Lease-push the fixed branch and write the baseline back.

    The tree may be clean already (a rebase --continue commits by itself)
    — only commit when there is something staged.
    """
    head_fork = os.getenv("HEAD_FORK", "")
    if not head_fork:
        ts_print("[pr-ci-watch] HEAD_FORK unset; cannot push the fix")
        return False
    if run_git(ascend_path, "status", "--porcelain").strip():
        # -s: upstream requires DCO — an unsigned fix commit fails the DCO
        # check and blocks the PR it was meant to unblock (PR #16424).
        run_git(ascend_path, "commit", "-s", "-m", message)
    _push_with_lease(ascend_path, branch)
    ts_print(f"[pr-ci-watch] pushed fix to {branch}")
    if with_baseline:
        try:
            _update_baseline_ref(ascend_path, head_fork, branch)
        except Exception as exc:
            ts_print(f"[pr-ci-watch] baseline write-back failed "
                     f"(non-fatal): {exc}")
    return True


def _adapter_fix(inputs: dict, round_dir: Path, session_id: str) -> dict:
    """One adapter-fix round; returns the AdaptResult fields we consume."""
    from main2main_flow.scripts.agent.opencode_adapter import (
        run_opencode_adapter,
    )
    result = run_opencode_adapter(inputs, session_id=session_id)
    out = {"session_id": result.session_id or "",
           "is_noop": bool(result.is_noop),
           "summary": result.step_summary or "",
           "modified_files": list(result.modified_files or [])}
    (round_dir / "adapter_result.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _adapter_payload(step_id: str, round_dir: Path, error_logs: list[str],
                     ascend_path: Path, base_sha: str, head_sha: str,
                     workspace_dir: Path) -> dict:
    """Same payload contract as the final quality gate's fix rounds."""
    return {
        "step_id": step_id,
        "previous_step_id": "",
        "previous_step_summary_path": "",
        "is_last_step": "true",
        "step_dir": str(round_dir),
        "patch_path": "",
        "changed_files_path": "",
        "ascend_path": str(ascend_path),
        "release_tag": _release_tag(ascend_path),
        "vllm_path": _workspace_vllm_path(workspace_dir),
        "role": "adapter-fix",
        "error_logs": json.dumps(error_logs, ensure_ascii=False),
        "code_structure_guide_file": _latest_structure_guide(workspace_dir),
        "mode": "adapter-fix",
        "start_commit": base_sha,
        "end_commit": head_sha,
        "vllm_report_context": _VLLM_REPORT_CTX,
    }


def _release_tag(ascend_path: Path) -> str:
    try:
        return (Path(ascend_path) / ".github" / "vllm-release-tag.commit"
                ).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _await_terminal(repo: str, head: str, *, deadline: float,
                    round_deadline: float, poll_sec: int,
                    pr_closed: Callable[[], bool]) -> tuple[str, list[dict]]:
    """Poll check-runs on one head sha until terminal.

    Returns (outcome, failing_checks); outcome is one of
    'terminal' | 'no-ci' | 'timeout' | 'pr-closed'.  'terminal' carries
    the failed check-runs (empty list = all green).
    """
    grace_end = time.monotonic() + NO_CI_GRACE_SEC
    while True:
        if pr_closed():
            return "pr-closed", []
        runs = _check_runs(repo, head)
        if runs:
            if all(c.get("status") == "completed" for c in runs):
                failing = [c for c in runs
                           if c.get("conclusion") not in _IGNORED_CONCLUSIONS]
                return "terminal", failing
        elif time.monotonic() > grace_end:
            wf = _gh_api(f"repos/{repo}/actions/runs"
                         f"?head_sha={head}&per_page=1")
            if not wf.get("workflow_runs"):
                return "no-ci", []
        if time.monotonic() > min(deadline, round_deadline):
            return "timeout", []
        time.sleep(poll_sec)


def _safe_name(check_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", check_name)[:80] or "job"


def _post_comment(repo: str, num: str, watch_dir: Path, status: str,
                  failing: list[dict], actions: list[str]) -> None:
    """Deterministic summary comment when the watcher gives up or the
    failures are not ours to fix (never on green — keep the PR quiet)."""
    lines = [f"[main2main] PR CI watcher stopped: **{status}**.", ""]
    if failing:
        lines.append("Failing checks:")
        lines += [f"- {c.get('name', '?')} ({c.get('conclusion', '?')})"
                  for c in failing[:10]]
        lines.append("")
    if actions:
        lines.append("Actions taken: " + "; ".join(actions[-10:]))
        lines.append("")
    lines.append(f"Evidence: workspace/{PR_CI_WATCH_DIR}/ "
                 "(uploaded run artifact).")
    body_path = watch_dir / "comment.md"
    body_path.write_text("\n".join(lines), encoding="utf-8")
    _run_gh(["pr", "comment", num, "--repo", repo,
             "--body-file", str(body_path)])


def watch_and_fix(pr_url: str, ascend_path: str | Path,
                  workspace_dir: str | Path,
                  *, poll_sec: int = 0, round_timeout_min: int = 0,
                  overall_timeout_min: int = 0, fix_rounds: int = -1,
                  reruns: int = -1, with_baseline: bool | None = None,
                  with_comment: bool | None = None,
                  session_id: str = "") -> dict:
    """Watch one PR's CI to a terminal verdict, repairing along the way.

    Budgets default from MAIN2MAIN_PR_WATCH_* env (see AGENTS.md).  Never
    raises: any internal failure lands in the returned status.
    """
    poll_sec = poll_sec or _env_int("MAIN2MAIN_PR_WATCH_POLL_SEC", 300)
    round_timeout_min = round_timeout_min or _env_int(
        "MAIN2MAIN_PR_WATCH_ROUND_TIMEOUT_MIN", 200)
    overall_timeout_min = overall_timeout_min or _env_int(
        "MAIN2MAIN_PR_WATCH_TIMEOUT_MIN", 360)
    if fix_rounds < 0:
        fix_rounds = _env_int("MAIN2MAIN_PR_WATCH_FIX_ROUNDS", 2)
    if reruns < 0:
        reruns = _env_int("MAIN2MAIN_PR_WATCH_RERUNS", 2)
    if with_baseline is None:
        with_baseline = _env_flag("MAIN2MAIN_PR_WATCH_BASELINE", "1")
    if with_comment is None:
        with_comment = _env_flag("MAIN2MAIN_PR_WATCH_COMMENT", "1")

    repo, num = _parse_pr_url(pr_url)
    ascend_path = Path(ascend_path)
    workspace_dir = Path(workspace_dir)
    watch_dir = workspace_dir / PR_CI_WATCH_DIR
    watch_dir.mkdir(parents=True, exist_ok=True)

    result = {"status": "", "rounds": 0, "actions": [], "pr_url": pr_url,
              "session_id": ""}
    fix_left, rerun_left = fix_rounds, reruns
    deadline = time.monotonic() + overall_timeout_min * 60
    max_rounds = fix_rounds + reruns + 2
    last_failing: list[dict] = []
    status = "exhausted"

    try:
        for round_no in range(max_rounds):
            result["rounds"] = round_no + 1
            pr = _pr_view(repo, num)
            if _pr_closed(pr):
                status = "pr-closed"
                break
            head = pr["head"]["sha"]
            base = pr["base"]["sha"]
            branch = pr["head"]["ref"]
            round_deadline = time.monotonic() + round_timeout_min * 60
            outcome, failing = _await_terminal(
                repo, head, deadline=deadline, round_deadline=round_deadline,
                poll_sec=poll_sec,
                pr_closed=lambda: _pr_closed(_pr_view(repo, num)))
            if outcome != "terminal":
                status = outcome
                break
            if not failing:
                status = "green" if round_no == 0 else "fixed"
                break
            last_failing = failing

            round_dir = watch_dir / f"round-{round_no}"
            round_dir.mkdir(parents=True, exist_ok=True)
            (round_dir / "checks.json").write_text(
                json.dumps(failing, indent=2), encoding="utf-8")

            # ci-gate fails whenever any upstream job fails — the real
            # signal lives in the non-aggregator checks.
            actionable = [c for c in failing
                          if not any(m in c.get("name", "").lower()
                                     for m in _AGGREGATOR_MARKERS)]
            if not actionable:
                last_failing = failing
                status = "exhausted"
                break

            # Pull every failing job's log once; classification and the
            # adapter both work off these files.
            logs: dict[str, str] = {}
            sigs: dict[str, str] = {}
            log_paths: dict[str, Path] = {}
            for check in actionable:
                name = check.get("name", "?")
                _, job_id = _parse_details_url(check.get("details_url", ""))
                # job_id in the name: long check names truncate to the same
                # 80-char prefix (e.g. matrix parts 2-4 / 3-4) — without it
                # the second fetch overwrites the first.
                log_path = round_dir / f"{_safe_name(name)[:64]}-{job_id}.log"
                logs[name] = (_fetch_job_logs(repo, job_id, log_path)
                              if job_id else "")
                log_paths[name] = log_path
                sigs[name] = classify_log(logs[name])

            actions = result["actions"]
            fork_url = ""
            if os.getenv("HEAD_FORK", ""):
                fork_url = f"https://github.com/{os.getenv('HEAD_FORK')}.git"

            # Deterministic repo-state failures poison everything
            # downstream (the cpu-ut rebase runs before the matrix) —
            # fix first, ignore the cascade in the same round.
            repo_state = [n for n, s in sigs.items() if s in ("rebase", "csrc")]
            if repo_state:
                if fix_left <= 0:
                    status = "exhausted"
                    break
                fix_left -= 1
                sig = "csrc-drift" if sigs[repo_state[0]] == "csrc" else "rebase"
                actions.append(f"round-{round_no}:{sig}-rebase")
                if fork_url and not _ensure_branch(ascend_path, branch,
                                                   fork_url):
                    status = "error"
                    break
                if not _fetch_upstream_main(ascend_path):
                    status = "error"
                    break
                rb = subprocess.run(["git", "rebase", UPSTREAM_FETCH_REF],
                                    cwd=str(ascend_path),
                                    capture_output=True, text=True,
                                    timeout=600)
                if rb.returncode != 0:
                    conflicts = run_git(ascend_path, "diff", "--name-only",
                                        "--diff-filter=U").split()
                    combined = run_git(ascend_path, "diff")
                    (round_dir / "conflict.diff").write_text(
                        combined, encoding="utf-8")
                    error_logs = [str(round_dir / "conflict.diff")] + [
                        str(ascend_path / f) for f in conflicts]
                    fix = _adapter_fix(
                        _adapter_payload(f"pr-ci-{sig}-{round_no}", round_dir,
                                         error_logs, ascend_path, base, head,
                                         workspace_dir),
                        round_dir, session_id)
                    session_id = fix["session_id"] or session_id
                    result["session_id"] = session_id
                    if fix["modified_files"] or conflicts:
                        run_git(ascend_path, "add", "-A")
                        cont = subprocess.run(
                            ["git", "-c", "core.editor=true",
                             "rebase", "--continue"],
                            cwd=str(ascend_path), capture_output=True,
                            text=True, timeout=600)
                        if cont.returncode != 0:
                            ts_print(f"[pr-ci-watch] rebase --continue "
                                     f"failed: {cont.stderr.strip()[-300:]}")
                            subprocess.run(["git", "rebase", "--abort"],
                                           cwd=str(ascend_path),
                                           capture_output=True, text=True)
                            status = "exhausted"
                            break
                    else:
                        subprocess.run(["git", "rebase", "--abort"],
                                       cwd=str(ascend_path),
                                       capture_output=True, text=True)
                        status = "exhausted"
                        break
                if fork_url:
                    if not _push_fix(ascend_path, branch, with_baseline,
                                     f"main2main: rebase onto main "
                                     f"(round-{round_no})"):
                        status = "error"
                        break
                else:
                    ts_print("[pr-ci-watch] HEAD_FORK unset; rebase resolved "
                             "locally but not pushed")
                    status = "error"
                    break
                continue

            # All failures infra-shaped -> one budgeted rerun of the same
            # run's failed jobs; same head sha, next round re-polls it.
            if all(s == "infra" for s in sigs.values()):
                if rerun_left <= 0:
                    status = "exhausted"
                    break
                rerun_left -= 1
                _rerun_failed(actionable)
                actions.append(f"round-{round_no}:rerun")
                continue

            # Content failures: own-diff triage over the failing jobs.
            diff_files = run_git(ascend_path, "diff", "--name-only",
                                 f"{base}...{head}").split()
            root_files: list[str] = []
            for text in logs.values():
                root_files.extend(extract_failure_files(text))
            root_files = list(dict.fromkeys(root_files))
            verdict = triage_own_diff(root_files, diff_files)
            (round_dir / "triage.json").write_text(
                json.dumps({"verdict": verdict, "root_files": root_files,
                            "diff_files": diff_files[:200]},
                           indent=2), encoding="utf-8")
            if verdict == "inherited":
                # Upstream-inherited (#16382 shape): not fixable inside
                # this PR — record and stop instead of burning rounds.
                actions.append(f"round-{round_no}:inherited")
                status = "inherited"
                break
            if fix_left <= 0:
                status = "exhausted"
                break
            fix_left -= 1
            actions.append(f"round-{round_no}:adapter-fix")
            if fork_url and not _ensure_branch(ascend_path, branch, fork_url):
                status = "error"
                break
            error_logs = [str(log_paths[n]) for n in logs if logs[n]]
            fix = _adapter_fix(
                _adapter_payload(f"pr-ci-content-{round_no}", round_dir,
                                 error_logs, ascend_path, base, head,
                                 workspace_dir),
                round_dir, session_id)
            session_id = fix["session_id"] or session_id
            result["session_id"] = session_id
            if fix["is_noop"] and "env-flake" in fix["summary"].lower():
                # The adapter read the logs and judged the failure
                # environmental; a rerun is the honest next step.
                if rerun_left <= 0:
                    status = "exhausted"
                    break
                rerun_left -= 1
                _rerun_failed(actionable)
                actions[-1] = f"round-{round_no}:rerun(env-flake)"
                continue
            if not fix["modified_files"] and not run_git(
                    ascend_path, "status", "--porcelain").strip():
                # No changes and no flake verdict: the next CI round
                # would replay the same failure.
                status = "exhausted"
                break
            if not fork_url:
                ts_print("[pr-ci-watch] HEAD_FORK unset; cannot push fix")
                status = "error"
                break
            if fix["modified_files"]:
                run_git(ascend_path, "add", "--", *fix["modified_files"])
            else:
                run_git(ascend_path, "add", "-A")
            if not _push_fix(ascend_path, branch, with_baseline,
                             f"main2main: fix PR CI (round-{round_no})"):
                status = "error"
                break
        else:
            status = "exhausted"
    except Exception as exc:
        ts_print(f"[pr-ci-watch] watcher error: {exc}")
        status = "error"
    finally:
        result["status"] = status
        ts_print(f"[pr-ci-watch] finished: {status} "
                 f"({result['rounds']} round(s), actions={result['actions']})")
        try:
            (watch_dir / PR_CI_WATCH_RESULT_FILE).write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8")
        except OSError:
            pass
        if (with_comment and status in ("exhausted", "timeout", "inherited")
                and last_failing):
            try:
                _post_comment(repo, num, watch_dir, status, last_failing,
                              result["actions"])
            except Exception as exc:
                ts_print(f"[pr-ci-watch] comment failed (non-fatal): {exc}")
    return result


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(
        description="Watch a main2main PR's CI and repair failures.")
    parser.add_argument("--pr-url", default="",
                        help=f"PR URL; defaults to {PR_URL_FILE}")
    parser.add_argument("--ascend-path", required=True)
    parser.add_argument("--workspace", default="")
    args = parser.parse_args()

    pr_url = args.pr_url
    if not pr_url and Path(PR_URL_FILE).exists():
        pr_url = Path(PR_URL_FILE).read_text(encoding="utf-8").strip()
    if not pr_url:
        parser.error("no PR url given and PR_URL_FILE missing")
    workspace = args.workspace or str(
        Path(__file__).parent.parent.parent / "workspace")
    watch_and_fix(pr_url, args.ascend_path, workspace)


if __name__ == "__main__":
    main()
