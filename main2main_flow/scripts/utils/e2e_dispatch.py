"""External E2E dispatcher: CPU-side control of the resident-runner (A2/A3/310P) E2E.

The main2main flow runs on a pure-CPU runner; E2E tests run on the NPU
runners via the single resident ``main2main-e2e.yaml`` workflow:

- at flow start the main workflow dispatches it once; each
  ``prepare-<chip>`` job builds the environment once (csrc cache, deps,
  editable install) and then enters a resident command loop for the whole
  main run.
- every E2E round, this module force-pushes the signal branch (accumulated
  patch + test_groups.json + command.json = round number + main run id) —
  that commit IS the round's command.  The resident jobs pick it up in
  place (no cache re-fetch, no reinstall, no new job), run the chip's
  tests with run_tests.py, and push the results to per-chip results
  branches (``<signal_branch>_results_<chip>``, layout
  ``round-<N>/<run_tests.py tests-dir files>``).

This module computes the test groups (select_tests.py, runner labels
rewritten to the main2main runners), pushes the commands, materializes the
per-chip ``round-<N>-result.json`` files (already classified by
run_tests.py's ci_log_summary pipeline) from the results branches, and
merges them into one run_tests()-shaped result dict — the result and
test-errors.txt are byte-identical for fix mode.
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from main2main_flow.scripts.utils.push_to_github import _push_via_proxy
from main2main_flow.scripts.utils.run_tests import aggregate_suite_results
from main2main_flow.scripts.utils.utils import run_git, ts_print

# PR CI runner label prefix -> main2main runner label + image_tag.
# Every group select_tests.py routes to a2/a3/310p runners is rewritten
# onto the three dedicated main2main runners.
_RUNNER_REWRITE: tuple[tuple[str, str, str], ...] = (
    ("linux-aarch64-a2b3-", "linux-aarch64-a2-1",
     "9.1.0-910b-ubuntu22.04-py3.12"),
    ("linux-aarch64-a3-", "linux-aarch64-a3-8-sh-001",
     "9.1.0-a3-ubuntu22.04-py3.12"),
    ("linux-aarch64-310p-", "linux-aarch64-310p-1",
     "9.1.0-310p-ubuntu22.04-py3.12"),
)


@dataclass
class E2EDispatchConfig:
    repo: str = "vllm-ascend-ci/vllm-ascend"
    workflow: str = "main2main-e2e.yaml"
    signal_branch: str = "main2main_e2e"
    # fork carrying the signal branch (the adapted code); empty = cfg.repo
    signal_repo: str = ""
    dispatch_ref: str = "main"
    flow_ref: str = "main"
    vllm: str = ""
    base_ref: str = "main"
    timeout_min: int = 480
    # run id of the dispatching main run; the resident loop only serves
    # commands whose command.json carries this id.
    main_run_id: str = ""
    # repo of the dispatching main run (stamped into command.json for the
    # runner-side completion probe); defaults to this workflow's repo.
    main_run_repo: str = ""
    # run id of the resident-runner prep run (the main workflow's pre-start
    # step dispatches it and records the id).  Used to probe the residents'
    # job status while waiting for round results — a dead prepare-<chip>-
    # job must fail the flow fast instead of waiting out the round timeout.
    prep_run_id: str = ""

    @classmethod
    def from_env(cls, target_commit: str = "") -> "E2EDispatchConfig":
        return cls(
            repo=os.getenv("MAIN2MAIN_E2E_REPO", "vllm-ascend-ci/vllm-ascend"),
            workflow=os.getenv("MAIN2MAIN_E2E_WORKFLOW", "main2main-e2e.yaml"),
            signal_branch=os.getenv("MAIN2MAIN_E2E_BRANCH", "main2main_e2e"),
            signal_repo=os.getenv("MAIN2MAIN_E2E_SIGNAL_REPO", ""),
            dispatch_ref=os.getenv("MAIN2MAIN_E2E_DISPATCH_REF", "main"),
            flow_ref=os.getenv("MAIN2MAIN_FLOW_REF", "main"),
            vllm=target_commit or os.getenv("TARGET_COMMIT", ""),
            base_ref=os.getenv("MAIN2MAIN_E2E_BASE_REF", "main"),
            timeout_min=int(os.getenv("MAIN2MAIN_E2E_TIMEOUT_MIN", "480")),
            main_run_id=os.getenv("MAIN2MAIN_E2E_MAIN_RUN_ID", ""),
            main_run_repo=os.getenv("GITHUB_REPOSITORY", ""),
            prep_run_id=os.getenv("MAIN2MAIN_E2E_PREP_RUN_ID", ""),
        )


def _rewrite_runner(label: str) -> tuple[str, str]:
    for prefix, new_label, image_tag in _RUNNER_REWRITE:
        if label.startswith(prefix):
            return new_label, image_tag
    return label, ""


# =============================================================================
# test group computation (CPU side, deterministic)
# =============================================================================

# Vendored select_tests trio (select_tests.py + test_config.yaml +
# runner_label.json), pinned verbatim from vllm-project/vllm-ascend
# @ 1e6e557bf — the last generation that accepted --changed-files and ran
# green in main2main runs (33406387872 / 33432455202).  Upstream's CI
# overhaul (#14793 series, 2026-09-01) replaced the contract with
# --test-list-file/--explicit-e2e-tests/--all-tests/--curated, deleted
# --changed-files, and moved the source->test mapping out of
# test_config.yaml into a coverage/SQLite pipeline whose artifacts the
# flow cannot access mid-run.  The flow passes UNCOMMITTED working-tree
# diffs, so it must keep the changed-file-driven generation; the vendor
# dir is self-contained (config + runner_label.json sit next to the
# script) and no longer depends on the ascend checkout's CI scripts.
_VENDOR_DIR = Path(__file__).resolve().parent / "vendor_select_tests"


def compute_test_groups(ascend_path: Path,
                        changed_files: list[str]) -> list[dict]:
    """Compute the ready-all test groups via the vendored select_tests.py.

    The changed files are passed explicitly via ``--changed-files``: the
    adaptation changes are UNCOMMITTED in the working tree (steps are
    committed only after their e2e round passes), so select_tests.py's
    ``--diff-base`` mode — which computes ``git diff base...HEAD`` — sees
    an empty diff and matches no modules (observed 2026-08-26: "no test
    groups for changed files").  No ``--pr-labels`` is passed: upstream
    #14793 removed the flag entirely (exit 2 otherwise, observed
    2026-08-31 run 33379683113), while the older generation treats labels
    as opt-in gating — absent labels select the full matched module set,
    which is exactly what the chip allowlist + MINIMAL filter then narrow
    down.  CPU groups are dropped (UT runs on the CPU runner via the
    final quality gate) and runner labels are rewritten onto the three
    main2main runners.
    """
    if not changed_files:
        return []
    select_script = _VENDOR_DIR / "select_tests.py"
    if not select_script.exists():
        raise RuntimeError(f"vendored select_tests.py not found at {select_script}")
    r = subprocess.run(
        [sys.executable, str(select_script), "--changed-files",
         *changed_files],
        cwd=str(ascend_path), capture_output=True, text=True,
        env={**os.environ, "GITHUB_OUTPUT": ""},  # force stdout output
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"select_tests.py failed (exit {r.returncode}): "
            f"{r.stderr.strip()[-2000:]}")
    groups_json = ""
    for line in r.stdout.strip().splitlines():
        if line.startswith("test_groups="):
            groups_json = line[len("test_groups="):]
            break
    if not groups_json:
        # select_tests exited 0 but emitted no test_groups= line — a broken
        # matcher (format change, wrong args), NOT "nothing to run".  Only
        # an explicit empty group list may be treated as no match.
        raise RuntimeError(
            "select_tests.py emitted no test_groups= line (exit 0); "
            f"stdout head: {r.stdout.strip()[:400]}")
    try:
        groups = json.loads(groups_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"unparseable test_groups output: {exc}")
    rewritten: list[dict] = []
    for g in groups:
        if g.get("npu_type") == "cpu":
            continue
        label, image_tag = _rewrite_runner(g.get("runner", ""))
        g = dict(g)
        g["runner"] = label
        if image_tag:
            g["image_tag"] = image_tag
        rewritten.append(g)
    allowed = set(chip_allowlist())
    rewritten = [g for g in rewritten if g.get("npu_type") in allowed]
    return apply_minimal_filter(rewritten)


def chip_allowlist() -> list[str]:
    """Chips that have resident jobs in main2main-e2e.yaml's matrix.

    Groups for chips outside the allowlist are dropped before dispatch: a
    chip with no resident job never pushes results, so every round would
    block on the full timeout.  Override via MAIN2MAIN_E2E_CHIPS
    (comma-separated) when the matrix changes.
    """
    raw = os.getenv("MAIN2MAIN_E2E_CHIPS", "a2,a3,310p")
    return [c.strip() for c in raw.split(",") if c.strip()]


def apply_minimal_filter(groups: list[dict]) -> list[dict]:
    """Keep only the tests named in MAIN2MAIN_E2E_MINIMAL (per-chip lines).

    Format: one line per chip — ``<chip>: <test> <test> ...``, where each
    test is a substring match on the test path.  Used for cheap validation
    runs on the fork: chips not named are dropped entirely, and named chips
    keep only the matching tests.  A line's chip decides the routing: the
    match scans ALL groups (not just that chip's) and each matched test is
    regrouped under the named chip with its original ``num_npus`` — this is
    how one_card tests (routed a2/310p by the ready-all suite) get served
    by the a3 resident (observed need 2026-08-28: 2 single-card + 1
    dual-card case on linux-aarch64-a3-800i-2).  The same test may thus
    appear under several chips.  No env → the full ready-all groups pass
    through unchanged.
    """
    spec = os.getenv("MAIN2MAIN_E2E_MINIMAL", "").strip()
    if not spec:
        return groups
    wanted: dict[str, list[str]] = {}
    for line in spec.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        chip, _, tests = line.partition(":")
        wanted[chip.strip()] = [t.strip() for t in tests.split() if t.strip()]
    out: list[dict] = []
    for chip, names in wanted.items():
        matched: dict[str, dict] = {}
        for g in groups:
            for t in g.get("tests", "").split():
                if t in matched:
                    continue
                if any(n in t for n in names):
                    matched[t] = g
        if not matched:
            continue
        by_cards: dict[int, list[str]] = {}
        for t in sorted(matched):
            by_cards.setdefault(
                int(matched[t].get("num_npus", 1)), []).append(t)
        for cards in sorted(by_cards):
            src = matched[by_cards[cards][0]]
            g = {k: src.get(k) for k in ("runner", "image_tag")}
            g["npu_type"] = chip
            g["num_npus"] = cards
            g["tests"] = " ".join(by_cards[cards])
            out.append(g)
    return out


def _map_changed_to_tests(ascend_path: Path,
                          changed_files: list[str]) -> list[str]:
    """Map changed source files to test paths via test_config.yaml modules.

    Uses the same ``source_file_dependencies`` prefix matching as
    select_tests.py, but deliberately ignores the ``optional`` flag: 62 of
    69 modules are ``optional: false`` (always-on), which makes
    select_tests' diff-based matching return the full suite for any
    non-empty diff — useless for incremental fix rounds.  The returned
    paths may be directories or files (including ``::nodeid`` suffixes);
    the caller intersects them with the ready-all full groups, so CPU-UT
    targets naturally fall away (the final quality gate runs those).
    """
    config_path = ascend_path / ".github/workflows/scripts/test_config.yaml"
    try:
        import yaml
        with open(config_path) as f:
            modules = list(yaml.safe_load_all(f))[0]
    except Exception:
        return []
    changed = set(changed_files)
    tests: set[str] = set()
    for module in modules:
        deps = module.get("source_file_dependencies") or []
        if any(f == dep or f.startswith(dep.rstrip("/") + "/")
               for f in changed for dep in deps):
            tests.update(module.get("tests") or [])
    for f in changed:
        if f.startswith("tests/") and f.endswith(".py"):
            tests.add(f)  # a changed test file runs itself
    return sorted(tests)


def incremental_test_groups(ascend_path: Path, base_sha: str,
                            failing: list[str],
                            full_groups: list[dict]) -> list[dict] | None:
    """Fix-round groups: failing tests ∪ tests hit by the new commits.

    Round 1 runs the full ready-all suite (baseline).  A fix round re-runs
    only (a) the failing tests — carried over from *full_groups* so their
    num_npus/npu_type routing stays authoritative — and (b) every NPU-suite test
    that test_config.yaml maps to the adapter's NEW commits since
    *base_sha* (the last dispatch HEAD): changed files → module
    ``source_file_dependencies`` → module tests, intersected with the
    ready-all groups so routing metadata is preserved.  This is what keeps
    a fix from silently breaking other cases: anything the new code
    touches re-runs, the round-1 full pass stands as baseline for
    everything else, and the final quality gate re-runs the full CPU UT
    suite on the final tree.

    Returns None when completeness cannot be guaranteed (a failing test
    that no full group maps — fall back to the full suite).
    """
    remaining = set(failing)
    groups: list[dict] = []
    for g in full_groups:
        keep = [t for t in g.get("tests", "").split() if t in remaining]
        if keep:
            ng = dict(g)
            ng["tests"] = " ".join(keep)
            groups.append(ng)
            remaining -= set(keep)
    if remaining:
        ts_print(f"[e2e-dispatch] {len(remaining)} failing test(s) "
                 f"unmappable to full groups: {sorted(remaining)}")
        return None
    changed = run_git(ascend_path, "diff", "--name-only", base_sha)
    try:
        mapped = _map_changed_to_tests(ascend_path, changed.splitlines()) \
            if changed.strip() else []
    except Exception as exc:
        ts_print(f"[e2e-dispatch] diff-impact mapping failed: {exc} — "
                 f"impact group empty")
        mapped = []
    if mapped:
        # Route mapped targets via the full groups (their ready-all
        # superset), expanding directories; targets absent from the NPU
        # suite (CPU UT) are dropped — the quality gate covers them.
        all_tests = _index_tests(full_groups)
        for target in mapped:
            # Config test targets may be files or directories (no trailing
            # slash); directories expand against the full group index.
            node = target.split("::")[0]
            if node in all_tests:
                candidates = [node]
            else:
                candidates = sorted(t for t in all_tests
                                    if t.startswith(node + "/"))
            for t in candidates:
                g = _group_of(full_groups, t)
                ng = dict(g)
                ng["tests"] = t
                groups.append(ng)
        n = sum(len(g["tests"].split()) for g in groups)
        ts_print(f"[e2e-dispatch] diff impact: {len(changed.splitlines())} "
                 f"file(s) -> {n} test(s) mapped")
    # Merge by routing key (npu_type, num_npus); dedup by test path.
    by_key = {}
    meta = {}
    for g in groups:
        key = (g.get("npu_type", ""), int(g.get("num_npus", 1)))
        meta.setdefault(key, {
            k: g.get(k) for k in ("npu_type", "num_npus", "runner",
                                  "image_tag")})
        by_key.setdefault(key, set()).update(g.get("tests", "").split())
    merged = []
    for key, tests in by_key.items():
        if not tests:
            continue
        g = dict(meta[key])
        g["tests"] = " ".join(sorted(tests))
        merged.append(g)
    covered = {t for g in merged for t in g["tests"].split()}
    if not set(failing) <= covered:
        ts_print("[e2e-dispatch] incremental groups failed completeness "
                 "check — falling back to full suite")
        return None
    return merged


def _index_tests(full_groups: list[dict]) -> set[str]:
    return {t for g in full_groups for t in g.get("tests", "").split()}


def _group_of(full_groups: list[dict], test: str) -> dict:
    for g in full_groups:
        if test in g.get("tests", "").split():
            return g
    raise KeyError(test)


# =============================================================================
# signal branch + dispatch
# =============================================================================

def _remote_branch_sha(wt: Path, fork: str, branch: str) -> str:
    """ls-remote the fork branch (token URL when available); '' on failure.

    *wt* is unused by the implementation (cwd for the ls-remote) but gives
    tests a seam: fakes can answer with the worktree's HEAD sha.
    """
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    url = (f"https://x-access-token:{token}@github.com/{fork}" if token
           else f"https://github.com/{fork}")
    try:
        r = subprocess.run(
            ["git", "ls-remote", url, f"refs/heads/{branch}"],
            cwd=str(wt), capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return ""
    if r.returncode != 0:
        return ""
    out = r.stdout.split()
    return out[0] if out else ""


def push_signal_branch(ascend_path: Path, branch: str, head_fork: str,
                       groups_json: list[dict], round_number: int,
                       main_run_id: str) -> str:
    """Force-push the round command to the fork's signal branch.

    The commit carries the accumulated patch (the working tree snapshot),
    test_groups.json, and command.json = {"round", "main_run_id"} — the
    resident runner jobs treat every new signal-branch commit for their
    main run as the command to serve that round.

    Uses a detached git worktree so the main working branch (the PR branch)
    is never touched — a commit here would leak into the PR squash.  Any
    uncommitted tracked changes in the main working tree (e.g. the final
    gate's format/mypy fixes) are carried into the snapshot via
    ``git diff HEAD`` + apply, so what gets tested is exactly the current
    working tree.  The signal branch is a throwaway communication channel
    (concurrency group main2main guarantees a single writer), so a plain
    ``--force`` is safe.  Returns the pushed commit sha.
    """
    ascend_path = Path(ascend_path)
    wt = Path(tempfile.mkdtemp(prefix="m2m-signal-"))
    sha = ""
    try:
        subprocess.run(
            ["git", "worktree", "add", "-f", "--detach", str(wt), "HEAD"],
            cwd=str(ascend_path), check=True, capture_output=True, text=True,
        )
        wt_patch = subprocess.run(
            ["git", "diff", "HEAD", "--binary"], cwd=str(ascend_path),
            capture_output=True, text=True,
        ).stdout
        if wt_patch.strip():
            # Explicit `-` reads the patch from stdin; --allow-empty is
            # avoided (only added in git 2.33, and the caller guarantees
            # the patch is non-empty).  On failure the patch head is
            # included so a mismatch is diagnosable from the run log.
            applied = subprocess.run(
                ["git", "apply", "-"], cwd=str(wt),
                input=wt_patch, capture_output=True, text=True,
            )
            if applied.returncode != 0:
                raise RuntimeError(
                    f"failed to apply working-tree changes to signal "
                    f"snapshot: {applied.stderr.strip()[-400:]}\n"
                    f"patch head:\n{wt_patch[:300]}")
        groups_path = wt / "test_groups.json"
        groups_path.write_text(
            json.dumps(groups_json, indent=2) + "\n", encoding="utf-8")
        (wt / "command.json").write_text(
            json.dumps({"round": round_number, "main_run_id": main_run_id})
            + "\n", encoding="utf-8")
        run_git(wt, "add", "-A")
        run_git(wt, "commit", "-m",
                "main2main: e2e signal (accumulated patch + test_groups)")
        # Re-commit as an orphan: the residents consume only the snapshot
        # tree (fetch -> command.json -> checkout --detach), never the
        # history.  A parentless commit keeps the push pack at tree-diff
        # size no matter how far the local checkout's history diverges
        # from the remote branch's — fresh mode once pushed a year of
        # upstream history through the CI proxy and died on HTTP 413
        # (run 33356185223).
        tree = run_git(wt, "rev-parse", "HEAD^{tree}").strip()
        committed = subprocess.run(
            ["git", "commit-tree", tree, "-m",
             "main2main: e2e signal (accumulated patch + test_groups)"],
            cwd=str(wt), capture_output=True, text=True, check=True,
        )
        sha = committed.stdout.strip()
        # Point the worktree's detached HEAD at the orphan commit so the
        # HEAD-based refspec below pushes it (and it survives gc).
        run_git(wt, "update-ref", "HEAD", sha)
        # The worktree is detached, so git cannot guess the refs/heads/
        # prefix for a shorthand dst (it only infers it when the src is a
        # ref under refs/{heads,tags}/); qualify it explicitly.
        refspec = f"HEAD:refs/heads/{branch}"
        # The force-push is idempotent, so verify-then-retry is safe: a
        # transient outage right after the push must not leave the round
        # unpushed (the residents would then wait out the full round
        # timeout for a command that never arrived).
        last_err: Exception | None = None
        for attempt in range(1, 6):
            try:
                _push_via_proxy(wt, head_fork, refspec, "--force")
            except Exception as exc:
                last_err = exc
                ts_print(f"[e2e-dispatch] signal push attempt {attempt}/5 "
                         f"failed: {exc}")
            else:
                verified = _remote_branch_sha(wt, head_fork, branch)
                if verified == sha:
                    break
                last_err = RuntimeError(
                    f"remote sha {verified or '<none>'} != pushed {sha}")
                ts_print(f"[e2e-dispatch] signal push attempt {attempt}/5: "
                         f"remote sha mismatch, retrying")
            time.sleep(min(60 * attempt, 300))
        else:
            raise RuntimeError(
                f"signal branch push failed after 5 attempts: {last_err}")
        ts_print(f"[e2e-dispatch] signal branch {head_fork}:{branch} "
                 f"pushed at {sha[:12]} (round {round_number} command)")
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt)],
            cwd=str(ascend_path), capture_output=True, text=True)
    return sha


def _gh_retry(args: list[str], retries: int = 4, base_delay: float = 8,
              timeout_s: int = 120) -> subprocess.CompletedProcess:
    """Run a gh command with retries + exponential backoff.

    Dispatch and artifact download cross the network; transient failures
    (5xx, timeouts, proxy blips) must not kill the run.  Fails hard only
    after all retries are exhausted.
    """
    last: subprocess.CompletedProcess | None = None
    for attempt in range(1, retries + 1):
        try:
            r = subprocess.run(args, capture_output=True, text=True,
                               timeout=timeout_s)
        except subprocess.TimeoutExpired:
            r = None
        if r is not None and r.returncode == 0:
            return r
        last = r
        if attempt < retries:
            delay = base_delay * (2 ** (attempt - 1))
            ts_print(f"[e2e-dispatch] gh {' '.join(args[:5])}... failed "
                     f"(attempt {attempt}/{retries}) — retrying in "
                     f"{delay:.0f}s")
            time.sleep(delay)
    detail = ""
    if last is not None:
        detail = (last.stderr.strip() or last.stdout.strip())[:500]
    raise RuntimeError(f"gh {' '.join(args[:5])}... failed after "
                       f"{retries} attempts: {detail or 'timeout'}")


def dispatch_workflow(repo: str, workflow_name: str, ref: str,
                      inputs: dict | None = None) -> int:
    """Dispatch a workflow_dispatch run and return its run id.

    The dispatch API answers 204 without a run id; poll the run list for
    the workflow's newest run on *ref* created after the POST.  The list
    API lags the POST by up to a minute (indexing delay), so allow a
    generous window; transient query failures just extend the wait.

    ``gh workflow run`` is used instead of ``gh api -F inputs=<json>``:
    on the self-hosted runners' old gh (2.4.0, Ubuntu 22.04 apt) ``-F``
    does not JSON-parse object values, so the API rejects them with
    HTTP 422 "is not an object" (observed on the 2026-08-26 main2main
    run — prep dispatch failed after 4 attempts, and no exec round was
    ever dispatched).
    """
    if not os.environ.get("GH_TOKEN"):
        raise RuntimeError("GH_TOKEN required for workflow dispatch")
    # The list API's created_at has second granularity, so the cutoff is
    # truncated to seconds and captured BEFORE the POST: the new run is
    # created during or after the POST, hence its created_at (truncated)
    # is always >= cutoff, while stale runs from earlier dispatches are
    # rejected.  The list endpoint lags the POST by up to a minute
    # (indexing delay), so poll generously.
    cutoff = int(time.time())
    args = ["gh", "workflow", "run", workflow_name, "--repo", repo,
            "--ref", ref]
    for key, value in (inputs or {}).items():
        args += ["-f", f"{key}={value}"]
    _gh_retry(args)
    deadline = time.time() + 180
    while time.time() < deadline:
        time.sleep(3)
        rr = subprocess.run(
            ["gh", "api",
             f"repos/{repo}/actions/runs?event=workflow_dispatch"
             f"&branch={ref}&per_page=10"],
            capture_output=True, text=True)
        if rr.returncode != 0:
            continue
        try:
            runs = json.loads(rr.stdout)["workflow_runs"]
        except (json.JSONDecodeError, KeyError):
            continue
        for run in runs:
            if not (run.get("path") and
                    run["path"].endswith(workflow_name)):
                continue
            created = run.get("created_at", "")
            if created and int(_parse_gh_time(created)) < cutoff:
                continue
            return int(run["id"])
    raise RuntimeError(f"no run found for {workflow_name} on {ref} "
                       f"(repo {repo})")


def _parse_gh_time(value: str) -> float:
    """Parse a GitHub ISO-8601 timestamp (e.g. 2026-08-26T16:42:59Z)."""
    from datetime import datetime
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def dispatch_prep(cfg: E2EDispatchConfig) -> int:
    """Fallback pre-start of the resident runners (the main workflow's
    pre-start step normally does this and passes the run id via env).

    The dispatched prepare-<chip> jobs build the env once and then serve
    every E2E round of this main run from their resident command loop.
    """
    run_id = dispatch_workflow(
        cfg.repo, cfg.workflow, cfg.dispatch_ref,
        {"vllm": cfg.vllm, "base_ref": cfg.base_ref,
         "flow_ref": cfg.flow_ref, "main_run_id": cfg.main_run_id,
         "main_run_repo": cfg.main_run_repo,
         "signal_branch": cfg.signal_branch,
         "signal_repo": cfg.signal_repo or cfg.repo})
    ts_print(f"[e2e-dispatch] resident runner run {run_id} started "
             f"(A2/A3/310P environments)")
    return run_id


# =============================================================================
# results fetch from the resident runners' results branches
# =============================================================================

def _signal_git_url(cfg: E2EDispatchConfig) -> str:
    """Token URL for the fork carrying the signal + results branches."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN", "")
    return (f"https://x-access-token:{token}@github.com/"
            f"{cfg.signal_repo or cfg.repo}")


_last_auth_warn = 0.0


def _warn_auth_failure(stderr: str) -> None:
    """Loud, throttled log for auth-class fetch failures.

    Unlike "branch not pushed yet", a 401/403 never recovers by polling —
    surface it (at most once per 10 min) instead of waiting out the round
    timeout in silence.
    """
    global _last_auth_warn
    s = (stderr or "").lower()
    if not any(k in s for k in ("authentication", "401", "403",
                                "invalid username", "could not read",
                                "permission")):
        return
    now = time.time()
    if now - _last_auth_warn < 600:
        return
    _last_auth_warn = now
    ts_print(f"[e2e-dispatch] results fetch AUTH FAILURE (check GH_TOKEN "
             f"scopes/expiry): {stderr.strip()[-300:]}")


def _fetch_round_results(git_url: str, branch: str, round_number: int,
                         dest: Path,
                         expect: dict | None = None) -> bool:
    """Materialize one chip's round results from its results branch.

    Branch layout: ``round-<N>/`` holding the run_tests.py tests-dir files
    (round-<N>-result.json, per-test logs/summaries, expected_tests.json)
    plus the resident's round-meta.json ({"round", "main_run_id",
    "command_sha"}).  When *expect* is given, the meta file must match —
    results left over from an earlier same-numbered round (a previous step,
    a chained run) must never be accepted as this round's verdict.  Returns
    False while the branch, the round path, or the identity check is not
    satisfied yet.
    """
    tmp = Path(tempfile.mkdtemp(prefix="m2m-results-fetch-"))
    try:
        def _git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(["git", *args], cwd=str(tmp),
                                  capture_output=True, text=True)

        if _git("init", "-q").returncode != 0:
            return False
        if _git("remote", "add", "origin", git_url).returncode != 0:
            return False
        fr = _git("fetch", "--force", "--no-tags", "--depth", "1", "origin",
                  branch)
        if fr.returncode != 0:
            # "branch not pushed yet" is the normal pending state; an
            # auth-class error never recovers on its own and must be
            # visible instead of silently polling out the whole budget.
            _warn_auth_failure(fr.stderr)
            return False
        path = f"round-{round_number}"
        if _git("cat-file", "-e", f"FETCH_HEAD:{path}").returncode != 0:
            return False  # command not served yet
        if expect:
            meta = _git("show", f"FETCH_HEAD:{path}/round-meta.json")
            if meta.returncode != 0:
                return False  # results not self-identifying yet
            try:
                got = json.loads(meta.stdout)
            except json.JSONDecodeError:
                return False
            if (got.get("main_run_id") != expect.get("main_run_id")
                    or got.get("command_sha") != expect.get("command_sha")):
                return False  # stale results from an earlier round
        arch = subprocess.run(
            ["git", "archive", "--format=tar", "FETCH_HEAD", path],
            cwd=str(tmp), capture_output=True)
        if arch.returncode != 0:
            return False
        # Never overlay fresh results onto a previous fetch of the same
        # round dir: stale logs / expected_tests.json would survive into
        # the parse.
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(arch.stdout)) as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                name = Path(member.name).relative_to(path)
                data = tf.extractfile(member)
                if data is not None:
                    (dest / name).write_bytes(data.read())
        return True
    except Exception:
        return False
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# Per-test progress lines run_tests.py writes to stdout — the resident
# mirrors them into round-<N>-progress.json on its results branch after
# every event: "  [<test>] started (N card(s))" and
# "  [<test>] done: exit=<rc>, result=<ci_result>, bugs=<n>, flakes=<n>".
_RE_TEST_STARTED = re.compile(r"\[([^\[\]]+)\] started \(\d+ card")
_RE_TEST_DONE = re.compile(
    r"\[([^\[\]]+)\] done: exit=(-?\d+), result=(\S+), "
    r"bugs=(\d+), flakes=(\d+)")


def _fetch_round_progress(git_url: str, branch: str,
                          round_number: int) -> list[dict] | None:
    """Read one chip's round-<N>-progress.json from its results branch.

    Returns the event list, or None while the branch / file is not there
    yet (nothing pushed so far, or final results already replaced the
    progress commit).  Any error degrades to None — progress relay is
    best-effort; the round results remain the authoritative verdict.
    """
    tmp = Path(tempfile.mkdtemp(prefix="m2m-progress-fetch-"))
    try:
        def _git(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(["git", *args], cwd=str(tmp),
                                  capture_output=True, text=True)

        if (_git("init", "-q").returncode != 0
                or _git("remote", "add", "origin", git_url).returncode != 0
                or _git("fetch", "--force", "--no-tags", "--depth", "1",
                        "origin", branch).returncode != 0):
            return None
        r = _git("show", f"FETCH_HEAD:round-{round_number}-progress.json")
        if r.returncode != 0:
            return None
        return list(json.loads(r.stdout).get("events", []))
    except Exception:
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _relay_test_progress(cfg: E2EDispatchConfig, round_number: int,
                         chips: list[str], state: dict) -> None:
    """Best-effort relay of per-test status into this workflow's log.

    The main workflow's log cannot natively show what happens inside the
    resident jobs, and the jobs' log API is not readable while a job is
    in progress — so the residents push every started/done event to
    round-<N>-progress.json on their results branch as the round runs,
    and this polls those files while the round results are pending,
    printing each event once.  Purely cosmetic.
    """
    if not chips:
        return
    git_url = _signal_git_url(cfg)
    for chip in chips:
        branch = f"{cfg.signal_branch}_results_{chip}"
        events = _fetch_round_progress(git_url, branch, round_number)
        if not events:
            continue
        seen: set = state.setdefault(chip, set())
        for e in events:
            test = e.get("test", "")
            event = e.get("event", "")
            if (test, event) in seen:
                continue
            seen.add((test, event))
            if event == "started":
                ts_print(f"[e2e-dispatch][{chip}] {test} started")
            elif event == "done":
                ts_print(f"[e2e-dispatch][{chip}] {test} done: "
                         f"exit={e.get('exit')}, result={e.get('result')}, "
                         f"bugs={e.get('bugs')}, flakes={e.get('flakes')}")


class ResidentRunnerError(RuntimeError):
    """A resident runner's prepare-<chip> job died (GitHub Actions job
    failure).  Not a test failure — there is no fix signal to extract; the
    flow process must die so the main run fails fast and the workflow's
    always() steps release the residents and upload the workspace."""


# A resident job in these conclusions is dead for this run (the resident
# loop keeps its job in_progress while serving; a completed+success job is
# the normal self-release after the main run finished — not a death).
_DEAD_CONCLUSIONS = frozenset(
    {"failure", "cancelled", "timed_out", "startup_failure", "skipped"})


def _probe_dead_residents(cfg: E2EDispatchConfig,
                          chips: list[str]) -> list[tuple[str, str]]:
    """Return (chip, conclusion) pairs whose prepare-<chip>- job died.

    Tolerant by design: a probe that cannot run (no prep run id, gh
    hiccup, parse error) returns [] — the next poll retries, and a
    transient probe failure must never kill the main run.  Only a
    definitive job death is reported.
    """
    if not cfg.prep_run_id or not chips:
        return []
    try:
        r = subprocess.run(
            ["gh", "api",
             f"repos/{cfg.repo}/actions/runs/{cfg.prep_run_id}/jobs"
             f"?per_page=100"],
            capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[:300])
        jobs = json.loads(r.stdout).get("jobs", [])
    except Exception as exc:
        ts_print(f"[e2e-dispatch] resident job probe failed (tolerated, "
                 f"will retry next poll): {exc}")
        return []
    dead: list[tuple[str, str]] = []
    for chip in chips:
        prefix = f"prepare-{chip}-"
        for job in jobs:
            name = job.get("name") or ""
            if not name.startswith(prefix):
                continue
            if (job.get("status") == "completed"
                    and job.get("conclusion") in _DEAD_CONCLUSIONS):
                dead.append((chip, job.get("conclusion") or "unknown"))
            break
    return dead


def wait_chip_results(cfg: E2EDispatchConfig, chips: list[str],
                      round_number: int, timeout_min: int,
                      ci_dir: Path,
                      command_sha: str = "") -> list[str]:
    """Wait for every chip's round results on the results branches.

    The resident jobs push each served round to
    ``<signal_branch>_results_<chip>``; poll until every chip's round path
    is materialized under *ci_dir* (or the budget expires — returns the
    chips that never reported).  *command_sha* (the sha push_signal_branch
    returned) binds the accepted results to THIS round's command — stale
    round dirs from earlier steps/runs are rejected until the resident
    re-serves the command.

    While waiting, per-test status lines are relayed from the residents'
    round-<N>-progress.json files into this log (see _relay_test_progress).
    """
    git_url = _signal_git_url(cfg)
    expect = ({"main_run_id": cfg.main_run_id, "command_sha": command_sha}
              if command_sha else None)
    pending = set(chips)
    relay_state: dict = {}
    deadline = time.time() + timeout_min * 60
    last_beat = time.time()
    while time.time() < deadline:
        done: list[str] = []
        for chip in sorted(pending):
            branch = f"{cfg.signal_branch}_results_{chip}"
            dest = ci_dir / f"main2main-e2e-round-{round_number}-{chip}"
            if _fetch_round_results(git_url, branch, round_number, dest,
                                    expect):
                done.append(chip)
                ts_print(f"[e2e-dispatch] round {round_number}: results "
                         f"received from {chip} ({branch})")
        pending -= set(done)
        if not pending:
            return []
        # Real-time death detection: a resident whose prepare-<chip> job
        # died (apt failure, OOM, infra kill) will never push results —
        # fail the run now instead of waiting out the round timeout.
        dead = _probe_dead_residents(cfg, sorted(pending))
        if dead:
            for chip, conclusion in dead:
                ts_print(f"::error::[e2e-dispatch] round {round_number}: "
                         f"resident runner for {chip} is DEAD "
                         f"(prepare-{chip}- job completed with "
                         f"conclusion={conclusion}) — it will never push "
                         f"results; failing the run")
            raise ResidentRunnerError(
                f"round {round_number}: resident runner job(s) died for "
                f"chips {dead} — no fix signal exists (this is not a test "
                f"failure)")
        _relay_test_progress(cfg, round_number, sorted(pending), relay_state)
        if time.time() - last_beat > 300:
            last_beat = time.time()
            ts_print(f"[e2e-dispatch] round {round_number}: still waiting "
                     f"for chips {sorted(pending)}")
        time.sleep(30)
    return sorted(pending)


# =============================================================================
# parsing (run_tests()-shaped result)
# =============================================================================

def _slug(test: str) -> str:
    return (test.replace("/", "__").replace(".py", "")
            .replace("::", "--"))


def _archive_round_artifacts(ci_dir: Path, round_number: int,
                             chip_entries: dict[str, tuple[Path,
                                                           list[dict]]]) -> None:
    """Flatten the per-chip round dirs into the main-branch ci_dir layout.

    Local run_tests() writes ``round-<N>-<slug>.log`` /
    ``round-<N>-<slug>-summary.json`` FLAT in ci_dir; the external channel
    materializes each chip under ``main2main-e2e-round-<N>-<chip>/``.  So
    the archived workspace artifact (and every log_path/summary_path the
    fix mode consumes) matches the main-branch layout, the per-test files
    are copied out flat, each entry's paths are rewritten to the flattened
    location, per-chip raw result jsons are kept as
    ``round-<N>-<chip>-result.json`` (the plain name belongs to the merged
    result, matching main's aggregate), and the chip dirs are removed.
    Cross-chip name collisions (one test routed to two chips, e.g. the
    MINIMAL validation set) keep the plain name for the first chip in
    sorted order and get a ``round-<N>-<chip>-<slug>`` suffix otherwise.
    """
    taken: set[str] = set()
    for chip in sorted(chip_entries):
        adir, entries = chip_entries[chip]
        for entry in entries:
            slug = _slug(entry["test"])
            base = f"round-{round_number}-{slug}"
            dest_base = (base if base not in taken
                         else f"round-{round_number}-{chip}-{slug}")
            taken.add(dest_base)
            if dest_base != base:
                ts_print(f"[e2e-dispatch] {chip}: log name collision for "
                         f"{slug} — archived as {dest_base}.log")
            for suffix, key in ((".log", "log_path"),
                                ("-summary.json", "summary_path")):
                src = adir / f"{base}{suffix}"
                dst = ci_dir / f"{dest_base}{suffix}"
                if src.exists():
                    shutil.copy2(src, dst)
                entry[key] = str(dst)
        raw = adir / f"round-{round_number}-result.json"
        if raw.exists():
            shutil.copy2(
                raw, ci_dir / f"round-{round_number}-{chip}-result.json")
    for adir, _ in chip_entries.values():
        shutil.rmtree(adir, ignore_errors=True)


def parse_exec_artifacts(ci_dir: Path, round_number: int,
                         step_id: int = 0) -> dict:
    """Merge per-chip run_tests() results into a run_tests()-shaped result.

    Fetched layout under *ci_dir* (transient, from the per-chip results
    branches via _fetch_round_results):
      main2main-e2e-round-<N>-<chip>/
        round-<N>-result.json          # run_tests() step-9 output (per chip)
        round-<N>-<slug>.log           # per-test logs
        round-<N>-<slug>-summary.json
        expected_tests.json            # [{"test","cards_required"}] filter output

    Each chip's ``round-<N>-result.json`` was already classified by
    run_tests.py with the same ci_log_summary pipeline as local runs — no
    re-classification happens here.  Suite entries are merged and
    re-aggregated identically to local run_tests() step 9, so the result
    dict and downstream test-errors.txt stay byte-identical for fix mode.
    On success the per-test files are re-archived FLAT into *ci_dir* with
    the exact main-branch names (see _archive_round_artifacts) and the
    chip dirs are removed.

    A chip whose job died before uploading (no round-<N>-result.json) is
    backfilled as NOT_RUN from ``expected_tests.json``: every test
    classifies as failed with a nonexistent log path — the job/group
    crashed before running it, so there is no fix signal to excerpt.
    """
    all_results: list[dict] = []
    rounds_info: list[dict] = []
    total_elapsed = 0.0
    chip_entries: dict[str, tuple[Path, list[dict]]] = {}

    for adir in sorted(ci_dir.glob(f"main2main-e2e-round-{round_number}-*")):
        if not adir.is_dir():
            continue
        chip = adir.name.rsplit("-", 1)[-1]
        chip_results: list[dict] = []
        result_path = adir / f"round-{round_number}-result.json"
        data = None
        if result_path.exists():
            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                # A killed run_tests.py can publish a partial json; the chip
                # must fall into the NOT_RUN backfill below, not vanish from
                # gating (dropping it would let the other chip's pass become
                # an overall pass).
                ts_print(f"[e2e-dispatch] {adir.name}: unparseable "
                         f"round-{round_number}-result.json ({exc}) — "
                         f"backfilling chip as NOT_RUN")
        if data is not None:
            for test, entry in data.get("suite_results", {}).items():
                entry = dict(entry)
                entry["test"] = test
                # The result JSON carries runner-side paths — point the
                # merged result at the downloaded artifact for now;
                # _archive_round_artifacts rewrites it to the flat
                # main-branch location.
                slug = _slug(test)
                entry["log_path"] = str(
                    adir / f"round-{round_number}-{slug}.log")
                entry["summary_path"] = str(
                    adir / f"round-{round_number}-{slug}-summary.json")
                all_results.append(entry)
                chip_results.append(entry)
            for rnd in data.get("rounds", []):
                rounds_info.append({**rnd, "chip": chip})
            total_elapsed = max(total_elapsed,
                                float(data.get("elapsed_s", 0.0)))
            chip_entries[chip] = (adir, chip_results)
            continue
        # No result json — the chip job failed before uploading.
        expected_path = adir / "expected_tests.json"
        if not expected_path.exists():
            ts_print(f"[e2e-dispatch] {adir.name}: no round-{round_number}-"
                     f"result.json and no expected_tests.json — dropped")
            continue
        try:
            expected = json.loads(expected_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            ts_print(f"[e2e-dispatch] {adir.name}: unparseable "
                     f"expected_tests.json ({exc}) — dropped")
            continue
        chip_tests: list[str] = []
        for item in expected:
            test = item.get("test", "")
            if not test:
                continue
            slug = _slug(test)
            all_results.append({
                "test": test,
                "cards_required": int(item.get("cards_required", 1)),
                "run_suite_exit_code": 1,
                "ci_result": "failed",
                "summary_error": "test not run — e2e job/group failed "
                                 "before reaching it (no log)",
                "code_bugs_count": 0, "env_flakes_count": 0,
                "failed_test_files_count": 0, "failed_test_cases_count": 0,
                "log_path": str(adir / f"round-{round_number}-"
                                      f"{slug}-NOT_RUN.log"),
                "summary_path": str(adir / f"round-{round_number}-"
                                          f"{slug}-NOT_RUN-summary.json"),
                "not_run": True,
            })
            chip_results.append(all_results[-1])
            chip_tests.append(test)
        rounds_info.append({"round": round_number, "chip": chip,
                            "tests": chip_tests, "elapsed_s": 0.0,
                            "not_run": True})
        ts_print(f"[e2e-dispatch] {chip}: {len(chip_tests)} test(s) NOT_RUN "
                 f"(job failed before uploading round-{round_number} "
                 f"results)")
        chip_entries[chip] = (adir, chip_results)

    if not all_results:
        return {"can_commit": False, "ci_result": "failed",
                "suite_results": {}, "summary_error": "no exec artifacts",
                "log_path": str(ci_dir), "summary_path": str(ci_dir),
                "round": round_number}

    _archive_round_artifacts(ci_dir, round_number, chip_entries)

    total_cards = max((r["cards_required"] for r in all_results), default=0)
    return aggregate_suite_results(
        step_id=step_id, round_number=round_number, all_results=all_results,
        total_cards=total_cards, sequential=False, remote=False,
        ci_dir=ci_dir, rounds_info=rounds_info, total_elapsed=total_elapsed,
    )


def run_external_e2e(cfg: E2EDispatchConfig, ascend_path: Path,
                     groups_json: list[dict], log_dir: Path,
                     round_number: int, step_id: int = 0,
                     push_before: bool = True,
                     timeout_min: int | None = None) -> dict:
    """Push the round command (signal branch), wait for the resident
    runners to serve it, parse the results.

    Returns the run_tests()-shaped result dict (see parse_exec_artifacts).
    """
    ci_dir = Path(log_dir) / str(step_id) / "tests"
    ci_dir.mkdir(parents=True, exist_ok=True)
    command_sha = ""
    if push_before:
        try:
            command_sha = push_signal_branch(
                ascend_path, cfg.signal_branch, cfg.signal_repo or cfg.repo,
                groups_json, round_number, cfg.main_run_id)
        except Exception as exc:
            # The push retries internally; reaching here means the command
            # channel is down.  Fail the round as a structured result (like
            # a chip timeout) instead of crashing the flow process.
            ts_print(f"[e2e-dispatch] round {round_number}: signal push "
                     f"failed: {exc}")
            return {"step_id": step_id, "round": round_number,
                    "ci_result": "failed", "can_commit": False,
                    "requires_fix": True, "suite_results": {},
                    "summary_error": f"round {round_number}: signal branch "
                                     f"push failed: {exc}",
                    "log_path": str(ci_dir), "summary_path": str(ci_dir),
                    "elapsed_s": 0.0, "rounds": []}
    allowed = set(chip_allowlist())
    chips = sorted({g.get("npu_type") for g in groups_json
                    if g.get("npu_type")} & allowed)
    if not chips:
        ts_print(f"[e2e-dispatch] round {round_number}: no resident chips "
                 f"in {len(groups_json)} group(s) — nothing to run")
        return {"step_id": step_id, "round": round_number,
                "ci_result": "passed", "can_commit": True,
                "suite_results": {}, "rounds": [], "elapsed_s": 0.0,
                "log_path": str(ci_dir), "summary_path": str(ci_dir)}
    timeout_min = timeout_min or cfg.timeout_min
    ts_print(f"[e2e-dispatch] round {round_number}: command pushed; "
             f"waiting for chips {chips} on the resident runners "
             f"(up to {timeout_min}min)")
    missing = wait_chip_results(cfg, chips, round_number, timeout_min,
                                ci_dir, command_sha=command_sha)
    if missing:
        return {"step_id": step_id, "round": round_number,
                "ci_result": "failed", "can_commit": False,
                "requires_fix": True, "suite_results": {},
                "summary_error": f"round {round_number}: no results from "
                                 f"chips {missing} after {timeout_min}min",
                "log_path": str(ci_dir), "summary_path": str(ci_dir),
                "elapsed_s": timeout_min * 60, "rounds": []}
    result = parse_exec_artifacts(ci_dir, round_number, step_id)
    result_path = ci_dir / f"round-{round_number}-result.json"
    result_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")
    ts_print(f"[e2e-dispatch] round {round_number}: {result['ci_result']} "
             f"(can_commit={result['can_commit']}, "
             f"{result.get('failed_test_files_count', 0)} failed file(s), "
             f"{result.get('failed_test_cases_count', 0)} failed case(s))")
    return result


# =============================================================================
# CLI (offline verification of the parser against downloaded artifacts)
# =============================================================================

def main() -> None:
    import argparse
    p = argparse.ArgumentParser(
        description="Offline-parse main2main-e2e artifacts into a "
                    "run_tests()-shaped result")
    p.add_argument("--ci-dir", type=Path, required=True)
    p.add_argument("--round", type=int, required=True)
    p.add_argument("--step-id", type=int, default=0)
    args = p.parse_args()
    result = parse_exec_artifacts(args.ci_dir, args.round, args.step_id)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result.get("can_commit", False) else 1)


if __name__ == "__main__":
    main()
