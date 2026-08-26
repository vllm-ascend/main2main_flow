"""External E2E dispatcher: CPU-side control of the three-runner (A2/A3/310P) E2E.

The main2main flow runs on a pure-CPU runner; E2E tests run on three GPU
runners via the single ``main2main-e2e.yaml`` workflow:

- round ``prep`` only runs the ``prepare-<chip>`` jobs (csrc cache, deps,
  editable install on the persistent runner filesystem) — dispatched at
  flow start so the first E2E round does not wait for setup.
- exec rounds (``round=N``) chain ``e2e-<chip>`` after ``prepare-<chip>``:
  the signal branch (accumulated patch + test_groups.json) is checked out
  and ``run_tests.py`` runs the chip's tests with card-packed parallel
  scheduling (same execution engine as the legacy main flow), uploading
  per-chip artifacts ``main2main-e2e-round-<N>-<chip>``.

This module computes the full ready-all test groups (select_tests.py,
runner labels rewritten to the three main2main runners), pushes them with
the accumulated adaptation patch to a signal branch on the fork, dispatches
and polls the workflow, downloads the artifacts, and merges the per-chip
``round-<N>-result.json`` files (already classified by run_tests.py's
ci_log_summary pipeline) into one run_tests()-shaped result dict — the
result and test-errors.txt are byte-identical for fix mode.
"""
from __future__ import annotations

import fnmatch
import json
import os
import subprocess
import sys
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
    ("linux-aarch64-a2b3-", "linux-aarch64-a2b1-8",
     "9.1.0-910b-ubuntu22.04-py3.12"),
    ("linux-aarch64-a3-", "linux-aarch64-a3-800i-16-cn12-001",
     "9.1.0-a3-ubuntu22.04-py3.12"),
    ("linux-aarch64-310p-", "linux-aarch64-310p-4",
     "9.1.0-310p-ubuntu22.04-py3.12"),
)


@dataclass
class E2EDispatchConfig:
    repo: str = "vllm-ascend-ci/vllm-ascend"
    workflow: str = "main2main-e2e.yaml"
    signal_branch: str = "main2main_e2e"
    dispatch_ref: str = "main"
    flow_ref: str = "main"
    vllm: str = ""
    base_ref: str = "main"
    timeout_min: int = 480

    @classmethod
    def from_env(cls, target_commit: str = "") -> "E2EDispatchConfig":
        return cls(
            repo=os.getenv("MAIN2MAIN_E2E_REPO", "vllm-ascend-ci/vllm-ascend"),
            workflow=os.getenv("MAIN2MAIN_E2E_WORKFLOW", "main2main-e2e.yaml"),
            signal_branch=os.getenv("MAIN2MAIN_E2E_BRANCH", "main2main_e2e"),
            dispatch_ref=os.getenv("MAIN2MAIN_E2E_DISPATCH_REF", "main"),
            flow_ref=os.getenv("MAIN2MAIN_FLOW_REF", "main"),
            vllm=target_commit or os.getenv("TARGET_COMMIT", ""),
            base_ref=os.getenv("MAIN2MAIN_E2E_BASE_REF", "main"),
            timeout_min=int(os.getenv("MAIN2MAIN_E2E_TIMEOUT_MIN", "480")),
        )


def _rewrite_runner(label: str) -> tuple[str, str]:
    for prefix, new_label, image_tag in _RUNNER_REWRITE:
        if label.startswith(prefix):
            return new_label, image_tag
    return label, ""


# =============================================================================
# test group computation (CPU side, deterministic)
# =============================================================================

def compute_test_groups(ascend_path: Path, base_sha: str,
                        changed_files: list[str]) -> list[dict]:
    """Compute the ready-all test groups via vllm-ascend's select_tests.py.

    Runs the same command as PR CI's ready-all flow (``--diff-base`` +
    ``--pr-labels ready-all``) on the local checkout — HEAD carries the
    accumulated adaptation patch, so ``git diff base...HEAD`` covers every
    gate fix.  CPU groups are dropped (UT runs on the CPU runner via the
    final quality gate) and runner labels are rewritten onto the three
    main2main runners.
    """
    select_script = ascend_path / ".github/workflows/scripts/select_tests.py"
    if not select_script.exists():
        raise RuntimeError(f"select_tests.py not found at {select_script}")
    r = subprocess.run(
        [sys.executable, str(select_script), "--diff-base", base_sha,
         "--filtered-changed-files-json", json.dumps(changed_files or []),
         "--pr-labels", "ready-all"],
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
        return []
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
    return apply_minimal_filter(rewritten)


def apply_minimal_filter(groups: list[dict]) -> list[dict]:
    """Keep only the tests named in MAIN2MAIN_E2E_MINIMAL (per-chip lines).

    Format: one line per chip — ``<chip>: <test> <test> ...``, where each
    test is a substring match on the test path.  Used for cheap validation
    runs on the fork: chips not named are dropped entirely, and named chips
    keep only the groups that contain a match (with the matching tests).
    No env → the full ready-all groups pass through unchanged.
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
    for g in groups:
        names = wanted.get(g.get("npu_type", ""))
        if not names:
            continue
        kept = [t for t in g.get("tests", "").split()
                if any(n in t for n in names)]
        if not kept:
            continue
        g = dict(g)
        g["tests"] = " ".join(kept)
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
    num_npus/npu_type routing stays authoritative — and (b) every GPU test
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
        # superset), expanding directories; targets absent from the GPU
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

def push_signal_branch(ascend_path: Path, branch: str, head_fork: str,
                       groups_json: list[dict]) -> str:
    """Force-push accumulated patch + test_groups.json to the fork branch.

    Uses a detached git worktree so the main working branch (the PR branch)
    is never touched — a commit here would leak into the PR squash.  Any
    uncommitted tracked changes in the main working tree (e.g. the final
    gate's format/mypy fixes) are carried into the snapshot via
    ``git diff HEAD`` + apply, so what gets tested is exactly the current
    working tree.  The signal branch is a throwaway communication channel
    (concurrency group main2main guarantees a single writer), so a plain
    ``--force`` is safe.  Returns the pushed commit sha.
    """
    import tempfile
    ascend_path = Path(ascend_path)
    wt = Path(tempfile.mkdtemp(prefix="m2m-signal-"))
    sha = ""
    try:
        subprocess.run(
            ["git", "worktree", "add", "-f", "--detach", str(wt), "HEAD"],
            cwd=str(ascend_path), check=True, capture_output=True, text=True,
        )
        wt_patch = subprocess.run(
            ["git", "diff", "HEAD"], cwd=str(ascend_path),
            capture_output=True, text=True,
        ).stdout
        if wt_patch.strip():
            applied = subprocess.run(
                ["git", "apply", "--allow-empty"], cwd=str(wt),
                input=wt_patch, capture_output=True, text=True,
            )
            if applied.returncode != 0:
                raise RuntimeError(
                    f"failed to apply working-tree changes to signal "
                    f"snapshot: {applied.stderr.strip()[-500:]}")
        groups_path = wt / "test_groups.json"
        groups_path.write_text(
            json.dumps(groups_json, indent=2) + "\n", encoding="utf-8")
        run_git(wt, "add", "-A")
        run_git(wt, "commit", "-m",
                "main2main: e2e signal (accumulated patch + test_groups)")
        sha = run_git(wt, "rev-parse", "HEAD").strip()
        # The worktree is detached, so git cannot guess the refs/heads/
        # prefix for a shorthand dst (it only infers it when the src is a
        # ref under refs/{heads,tags}/); qualify it explicitly.
        _push_via_proxy(wt, head_fork, f"HEAD:refs/heads/{branch}", "--force")
        ts_print(f"[e2e-dispatch] signal branch {head_fork}:{branch} "
                 f"pushed at {sha[:12]}")
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
    """
    if not os.environ.get("GH_TOKEN"):
        raise RuntimeError("GH_TOKEN required for workflow dispatch")
    args = ["gh", "api", "-X", "POST",
            f"repos/{repo}/actions/workflows/{workflow_name}/dispatches",
            "-f", f"ref={ref}"]
    if inputs:
        # -F (typed field) parses the JSON into an object; -f would send
        # the JSON as a string, and the API rejects inputs with HTTP 422
        # "is not an object".
        args += ["-F", f"inputs={json.dumps(inputs)}"]
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
            if run.get("path") and run["path"].endswith(workflow_name):
                return int(run["id"])
    raise RuntimeError(f"no run found for {workflow_name} on {ref} "
                       f"(repo {repo})")


def wait_for_run(repo: str, run_id: int, timeout_min: int,
                 poll_s: int = 60) -> dict:
    """Poll the run until completed; on timeout cancel it to free runners."""
    deadline = time.time() + timeout_min * 60
    while time.time() < deadline:
        r = subprocess.run(
            ["gh", "api", f"repos/{repo}/actions/runs/{run_id}"],
            capture_output=True, text=True)
        if r.returncode == 0:
            try:
                run = json.loads(r.stdout)
            except json.JSONDecodeError:
                run = {}
            if run.get("status") == "completed":
                return run
        time.sleep(poll_s)
    subprocess.run(
        ["gh", "api", "-X", "POST",
         f"repos/{repo}/actions/runs/{run_id}/cancel"],
        capture_output=True, text=True)
    ts_print(f"[e2e-dispatch] run {run_id} timed out after "
             f"{timeout_min}min, cancelled")
    return {"status": "timed_out", "conclusion": "timed_out",
            "run_id": run_id}


def dispatch_prep(cfg: E2EDispatchConfig) -> int:
    """Pre-start the three runners' environment prep alongside the main flow.

    ``round=prep`` on main2main-e2e.yaml runs only the ``prepare-<chip>``
    jobs (the ``e2e-<chip>`` jobs are guarded by ``round != prep``), so the
    first E2E round reuses the prepared env instead of building it.
    """
    run_id = dispatch_workflow(
        cfg.repo, cfg.workflow, cfg.dispatch_ref,
        {"vllm": cfg.vllm, "base_ref": cfg.base_ref, "round": "prep",
         "flow_ref": cfg.flow_ref})
    ts_print(f"[e2e-dispatch] prep workflow run {run_id} started "
             f"(A2/A3/310P environments)")
    return run_id


def wait_prep(cfg: E2EDispatchConfig, run_id: int,
              timeout_min: int | None = None) -> dict:
    """Wait for the prep workflow; the first E2E round needs it ready."""
    return wait_for_run(cfg.repo, run_id, timeout_min or cfg.timeout_min)


# =============================================================================
# artifact download + parsing (run_tests()-shaped result)
# =============================================================================

def _download_artifacts(repo: str, run_id: int, pattern: str,
                        dest_dir: Path) -> None:
    """Download the round artifacts with retries and a wait budget.

    Artifacts of a just-completed run can take seconds to minutes to be
    finalized (and listed) on GitHub, and the download itself crosses the
    network — both need retries with backoff.  Fails hard only after the
    wait budget is exhausted.
    """
    names: list[str] = []
    matches: list[str] = []
    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            r = _gh_retry(
                ["gh", "api", "--paginate",
                 f"repos/{repo}/actions/runs/{run_id}/artifacts"],
                retries=3, base_delay=5)
            data = json.loads(r.stdout)
            names = [a["name"] for a in data.get("artifacts", [])
                     if not a.get("expired", False)]
        except Exception as exc:
            ts_print(f"[e2e-dispatch] run {run_id}: artifact list query "
                     f"failed ({exc}) — retrying")
            time.sleep(15)
            continue
        matches = [n for n in names if fnmatch.fnmatch(n, pattern)]
        if matches:
            break
        ts_print(f"[e2e-dispatch] run {run_id}: artifacts matching "
                 f"{pattern!r} not finalized yet (listed: "
                 f"{names or 'none'}) — waiting 15s")
        time.sleep(15)
    if not matches:
        raise RuntimeError(f"no artifacts matching {pattern!r} for run "
                           f"{run_id} after 600s (listed: {names})")
    dest_dir.mkdir(parents=True, exist_ok=True)
    got: list[str] = []
    for attempt in range(1, 6):
        _gh_retry(["gh", "run", "download", "--repo", repo, str(run_id),
                   "--pattern", pattern, "-D", str(dest_dir)],
                  retries=2, base_delay=8)
        got = [p.name for p in dest_dir.glob(f"{pattern}*") if p.is_dir()]
        if len(got) >= len(matches):
            return
        ts_print(f"[e2e-dispatch] run {run_id}: download returned but only "
                 f"{len(got)}/{len(matches)} artifact dir(s) present "
                 f"(attempt {attempt}/5) — retrying")
        time.sleep(15 * attempt)
    raise RuntimeError(f"artifact download incomplete for run {run_id}: "
                       f"got {len(got)}/{len(matches)} dirs")


def _slug(test: str) -> str:
    return (test.replace("/", "__").replace(".py", "")
            .replace("::", "--"))


def parse_exec_artifacts(ci_dir: Path, round_number: int,
                         step_id: int = 0) -> dict:
    """Merge per-chip run_tests() results into a run_tests()-shaped result.

    Expected layout under *ci_dir* (artifacts uploaded from main2main-e2e.yaml):
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

    A chip whose job died before uploading (no round-<N>-result.json) is
    backfilled as NOT_RUN from ``expected_tests.json``: every test
    classifies as failed with a nonexistent log path — the job/group
    crashed before running it, so there is no fix signal to excerpt.
    """
    all_results: list[dict] = []
    rounds_info: list[dict] = []
    total_elapsed = 0.0

    for adir in sorted(ci_dir.glob(f"main2main-e2e-round-{round_number}-*")):
        if not adir.is_dir():
            continue
        chip = adir.name.rsplit("-", 1)[-1]
        result_path = adir / f"round-{round_number}-result.json"
        if result_path.exists():
            try:
                data = json.loads(result_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError as exc:
                ts_print(f"[e2e-dispatch] {adir.name}: unparseable "
                         f"round-{round_number}-result.json ({exc}) — "
                         f"chip results dropped")
                continue
            for test, entry in data.get("suite_results", {}).items():
                entry = dict(entry)
                entry["test"] = test
                # The result JSON carries runner-side paths — point the
                # merged result at the downloaded artifact instead.
                slug = _slug(test)
                entry["log_path"] = str(
                    adir / f"round-{round_number}-{slug}.log")
                entry["summary_path"] = str(
                    adir / f"round-{round_number}-{slug}-summary.json")
                all_results.append(entry)
            for rnd in data.get("rounds", []):
                rounds_info.append({**rnd, "chip": chip})
            total_elapsed = max(total_elapsed,
                                float(data.get("elapsed_s", 0.0)))
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
            chip_tests.append(test)
        rounds_info.append({"round": round_number, "chip": chip,
                            "tests": chip_tests, "elapsed_s": 0.0,
                            "not_run": True})
        ts_print(f"[e2e-dispatch] {chip}: {len(chip_tests)} test(s) NOT_RUN "
                 f"(job failed before uploading round-{round_number} "
                 f"results)")

    if not all_results:
        return {"can_commit": False, "ci_result": "failed",
                "suite_results": {}, "summary_error": "no exec artifacts",
                "log_path": str(ci_dir), "summary_path": str(ci_dir),
                "round": round_number}

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
    """Push the signal branch, dispatch the exec workflow, parse the result.

    Returns the run_tests()-shaped result dict (see parse_exec_artifacts).
    """
    ci_dir = Path(log_dir) / str(step_id) / "tests"
    ci_dir.mkdir(parents=True, exist_ok=True)
    if push_before:
        push_signal_branch(ascend_path, cfg.signal_branch, cfg.repo,
                           groups_json)
    inputs = {"vllm": cfg.vllm, "base_ref": cfg.base_ref,
              "round": str(round_number),
              "signal_branch": cfg.signal_branch,
              "flow_ref": cfg.flow_ref}
    run_id = dispatch_workflow(cfg.repo, cfg.workflow, cfg.dispatch_ref,
                               inputs)
    ts_print(f"[e2e-dispatch] exec run {run_id} started (round "
             f"{round_number})")
    run = wait_for_run(cfg.repo, run_id, timeout_min or cfg.timeout_min)
    if run.get("status") == "timed_out":
        return {"step_id": step_id, "round": round_number,
                "ci_result": "failed", "can_commit": False,
                "requires_fix": True, "suite_results": {},
                "summary_error": f"e2e run {run_id} timed out after "
                                 f"{timeout_min or cfg.timeout_min}min "
                                 f"(cancelled)",
                "log_path": str(ci_dir), "summary_path": str(ci_dir),
                "elapsed_s": (timeout_min or cfg.timeout_min) * 60,
                "rounds": []}
    if run.get("conclusion") != "success":
        ts_print(f"[e2e-dispatch] run {run_id} conclusion="
                 f"{run.get('conclusion')}")
    _download_artifacts(cfg.repo, run_id,
                        f"main2main-e2e-round-{round_number}-*", ci_dir)
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
