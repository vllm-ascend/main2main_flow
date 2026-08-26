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
    rounds: int = 3

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
            rounds=int(os.getenv("MAIN2MAIN_E2E_ROUNDS", "3")),
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
    return rewritten


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
        _push_via_proxy(wt, head_fork, f"HEAD:{branch}", "--force")
        ts_print(f"[e2e-dispatch] signal branch {head_fork}:{branch} "
                 f"pushed at {sha[:12]}")
    finally:
        subprocess.run(
            ["git", "worktree", "remove", "--force", str(wt)],
            cwd=str(ascend_path), capture_output=True, text=True)
    return sha


def dispatch_workflow(repo: str, workflow_name: str, ref: str,
                      inputs: dict | None = None) -> int:
    """Dispatch a workflow_dispatch run and return its run id.

    The dispatch API answers 204 without a run id; poll the run list for
    the workflow's newest run on *ref* created after the POST.
    """
    if not os.environ.get("GH_TOKEN"):
        raise RuntimeError("GH_TOKEN required for workflow dispatch")
    args = ["gh", "api", "-X", "POST",
            f"repos/{repo}/actions/workflows/{workflow_name}/dispatches",
            "-f", f"ref={ref}"]
    if inputs:
        args += ["-f", f"inputs={json.dumps(inputs)}"]
    r = subprocess.run(args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"workflow dispatch failed: {r.stderr.strip() or r.stdout.strip()}")
    deadline = time.time() + 60
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
    r = subprocess.run(
        ["gh", "run", "download", "--repo", repo, str(run_id),
         "--pattern", pattern, "-D", str(dest_dir)],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"gh run download failed: {r.stderr.strip() or r.stdout.strip()}")


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
