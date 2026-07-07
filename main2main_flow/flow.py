
import glob
import json
import os
import re
import shutil
import subprocess
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from crewai.flow import Flow, listen, start, router

from main2main_flow.agent.opencode_adapter import (
    AdaptResult, run_opencode_adapter, run_opencode_review,
)
from main2main_flow.scripts.detect_commits import detect
from main2main_flow.scripts.plan_steps import run_plan
from main2main_flow.scripts.pre_ci_check import run_check
from main2main_flow.scripts.push_to_github import push_and_create_pr
from main2main_flow.scripts.run_tests import run_tests
from main2main_flow.scripts.update_commit_reference import run_update
from main2main_flow.utils import (
    UpgradeCompleted, UpgradeFailed,
    HasCommit, HasNoCommit, resolve_path, PROJECT_ROOT, WORKSPACE_DIR, DETECT_FILE,
    STEPS_FILE, FINAL_SUMMARY_FILE, FINAL_TARGET_PATCH_FILE,
    STEPS_DIR, VLLM_GIT_PATCH_FILE, VLLM_GIT_CHANGED_FILES, PRE_CI_CHECK_FILE,
    EACH_STEP_SUMMARY_FILE, EACH_STEP_TARGET_PATCH_FILE, EACH_STEP_CODE_STRUCTURE_GUIDE_FILE,
    EACH_STEP_REVIEW_FILE, FINAL_CODE_STRUCTURE_GUIDE_FILE, RUNS_LEDGER_FILE,
    REFERENCE_CANDIDATES_FILE, StepFailure, git_intent_to_add, is_tree_clean,
    tree_status_excerpt, run_git, ts_print,
)

_REFERENCE_DIR = str(Path(__file__).parent / "reference")

TRACKING_FILE = ".github/vllm-main-verified.commit"


def _parse_test_cases_env() -> list[str] | None:
    val = os.getenv("MAIN2MAIN_TEST_CASES", "").strip()
    if not val:
        return None
    return [t.strip() for t in val.replace("\n", " ").split() if t.strip()]


def _review_checklist() -> str:
    """§9 of review-lessons.md — the operative checklist fed to the critic."""
    path = Path(_REFERENCE_DIR) / "review-lessons.md"
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8")
    idx = text.find("## 9.")
    return text[idx:] if idx != -1 else ""


def _patch_sections(patch_text: str) -> dict[str, str]:
    """Split a git patch into per-file sections keyed by filename."""
    sections: dict[str, str] = {}
    fname: str | None = None
    buf: list[str] = []
    for line in patch_text.splitlines():
        if line.startswith("diff --git a/"):
            if fname is not None:
                sections[fname] = "\n".join(buf)
            fname = line.split()[-1][2:]
            buf = []
        elif fname is not None:
            buf.append(line)
    if fname is not None:
        sections[fname] = "\n".join(buf)
    return sections


def _files_touched_between(prev_patch_text: str, cur_patch_text: str) -> set[str]:
    """Files whose per-file diff section is new or changed vs the previous
    cumulative patch — i.e. the files this step actually touched."""
    prev = _patch_sections(prev_patch_text)
    cur = _patch_sections(cur_patch_text)
    return {f for f, section in cur.items() if prev.get(f) != section}


def _patch_new_files(patch_text: str) -> list[str]:
    """Files a patch creates on disk: ``new file mode`` sections plus
    rename/copy destinations (both are untracked after reset + checkout)."""
    created: list[str] = []
    for fname, section in _patch_sections(patch_text).items():
        if section.startswith("new file mode"):
            created.append(fname)
            continue
        for line in section.splitlines():
            # Extended-header lines are unprefixed; hunk content lines carry
            # a +/-/space prefix, so this cannot match file contents.
            if line.startswith(("rename to ", "copy to ")):
                created.append(line.partition(" to ")[2])
                break
    return created


class Main2MainState(BaseModel):
    vllm_path: str = ""
    vllm_ascend_path: str = ""
    target_commit: str = ""

    steps: list = []
    release_tag: str = ""

    total_steps: int = 0
    current_step: int = 0

    cur_vllm_commit: str = ""
    cur_ascend_commit: str = ""
    cur_patch_path: str = ""

    original_vllm_ref: str = ""
    original_ascend_ref: str = ""

    test_errors: list = []
    retry_count: int = 0

    final_status: str = ""
    failure_reason: str = ""

    # Updated only when e2e passes; the commit the run actually verified
    last_verified_vllm_commit: str = ""
    # Trimmed error content captured at the last e2e failure
    last_error_excerpt: str = ""

    # Tracked from detect step for PR title / push
    base_commit: str = ""

    # Changed files from current adaptation step (for precise test selection)
    changed_files: list[str] = []
    # Cumulative changed-file list after the previous verified step
    prev_step_files: list[str] = []


class Main2MainFlow(Flow[Main2MainState]):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._deadline = 0.0

    @start()
    def initialize(self):
        """Initialize state; all paths default to workspace/ under the project root."""
        if WORKSPACE_DIR.exists():
            shutil.rmtree(WORKSPACE_DIR)
        WORKSPACE_DIR.mkdir(parents=True)

        raw_vllm = (self.state.vllm_path
                    or os.getenv("VLLM_PATH")
                    or str(WORKSPACE_DIR / "repos" / "vllm"))
        raw_ascend = (self.state.vllm_ascend_path
                      or os.getenv("VLLM_ASCEND_PATH")
                      or str(WORKSPACE_DIR / "repos" / "vllm-ascend"))

        self.state.vllm_path = resolve_path(raw_vllm, "vllm")
        self.state.vllm_ascend_path = resolve_path(raw_ascend, "vllm-ascend")
        self.state.target_commit = (
            self.state.target_commit or os.getenv("VLLM_TARGET_COMMIT", "")
        )

        allow_dirty = os.getenv("MAIN2MAIN_ALLOW_DIRTY", "false").lower() == "true"
        for name, repo in (("vllm", self.state.vllm_path),
                           ("vllm-ascend", self.state.vllm_ascend_path)):
            if is_tree_clean(repo):
                continue
            excerpt = tree_status_excerpt(repo)
            if allow_dirty:
                ts_print(f"[init] WARNING: {name} tree at {repo} is DIRTY — "
                         f"continuing because MAIN2MAIN_ALLOW_DIRTY=true:\n{excerpt}")
            else:
                raise RuntimeError(
                    f"{name} working tree at {repo} is dirty; commit/stash the "
                    f"changes or set MAIN2MAIN_ALLOW_DIRTY=true:\n{excerpt}"
                )

        target_ref = os.getenv("MAIN2MAIN_TARGET_REF", "")
        if target_ref and not self.state.target_commit:
            run_git(self.state.vllm_path, "fetch", "origin", "--tags", "--force")
            self.state.target_commit = run_git(
                self.state.vllm_path, "rev-parse", target_ref
            ).strip()
            ts_print(f"[init] resolved MAIN2MAIN_TARGET_REF '{target_ref}' → "
                     f"{self.state.target_commit[:8]}")

        vllm_branch = run_git(self.state.vllm_path, "branch", "--show-current").strip()
        self.state.original_vllm_ref = vllm_branch or run_git(self.state.vllm_path, "rev-parse", "HEAD").strip()
        ascend_branch = run_git(self.state.vllm_ascend_path, "branch", "--show-current").strip()
        self.state.original_ascend_ref = ascend_branch or run_git(self.state.vllm_ascend_path, "rev-parse", "HEAD").strip()

        max_hours = float(os.getenv("MAIN2MAIN_MAX_HOURS", "12"))
        self._deadline = time.monotonic() + max_hours * 3600 if max_hours > 0 else 0.0

    @router(initialize)
    def analyze_commit_and_plan_step(self) -> Literal["HasCommit", "HasNoCommit"]:
        vllm_path = Path(self.state.vllm_path)
        vllm_ascend_path = Path(self.state.vllm_ascend_path)

        # generate detect.json in workspace
        result, has_commit = detect(vllm_path, vllm_ascend_path,
                                    self.state.target_commit or None)
        self.state.release_tag = result.get("compat_tag") or ""
        self.state.base_commit = result.get("base_commit", "")

        (WORKSPACE_DIR / DETECT_FILE).write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        ts_print(f"[analyze] base={result['base_commit'][:8]}  "
              f"target={result['target_commit'][:8]}")

        if not has_commit:
            return HasNoCommit

        # generate steps.json + per-step upstream.patch/changed_files.txt in workspace
        plan = run_plan(vllm_path, result["base_commit"], result["target_commit"],
                        steps_dir=WORKSPACE_DIR / STEPS_DIR)
        self.state.steps = plan["steps"]
        self.state.total_steps = len(plan["steps"])

        (WORKSPACE_DIR / STEPS_FILE).write_text(
            json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        if self.state.total_steps == 0:
            return HasNoCommit

        ts_print(f"[analyze] planned {self.state.total_steps} step(s) covering "
              f"{plan['total_commits']} commit(s).")
        ts_print("===========================================")
        ts_print(json.dumps(plan["steps"], indent=2, ensure_ascii=False))

        return HasCommit

    @listen(HasNoCommit)
    def has_no_commit(self):
        ts_print("[done] repos already in sync, nothing to adapt — flow finished.")

    @listen(HasCommit)
    def process_steps(self):
        while self.state.current_step < self.state.total_steps:
            if self._deadline and time.monotonic() > self._deadline:
                self.state.final_status = UpgradeFailed
                self.state.failure_reason = "time budget exceeded"
                ts_print("[process_steps] global time budget exceeded, stopping")
                break

            try:
                ai_ok = self._ai_analysis()
            except StepFailure as exc:
                ai_ok = False
                self.state.failure_reason = str(exc)
            except Exception as exc:
                # Never crash the flow: restoration in generate_final_post must run.
                ai_ok = False
                self.state.failure_reason = f"unexpected: {exc!r}"
                ts_print(traceback.format_exc())

            if not ai_ok:
                self.state.retry_count += 1
                if self.state.retry_count >= 3:
                    self.state.final_status = UpgradeFailed
                    self.state.failure_reason = (
                        self.state.failure_reason or "step failed after 3 retries"
                    )
                    break
                continue

            try:
                test_pass = self._run_e2e_test()
            except Exception as exc:
                test_pass = False
                self.state.failure_reason = f"e2e crashed: {exc!r}"
                ts_print(traceback.format_exc())

            if test_pass:
                if self.state.retry_count > 0:
                    self._record_fix_candidate()
                self.state.last_verified_vllm_commit = self.state.cur_vllm_commit
                self.state.prev_step_files = list(self.state.changed_files)
                self.state.current_step += 1
                self.state.retry_count = 0
                # a recovered retry must not misreport a completed run as failed
                self.state.failure_reason = ""
            else:
                self.state.retry_count += 1
                if self.state.retry_count >= 3:
                    self.state.final_status = UpgradeFailed
                    self.state.failure_reason = (
                        self.state.failure_reason or "step failed after 3 retries"
                    )
                    break
        else:
            self.state.final_status = UpgradeCompleted

    def _ai_analysis(self) -> bool:
        """Run the adapt/fix agent + pre-CI + critic for the current step.

        Returns True when the step is ready for e2e; raises StepFailure when
        the attempt budget is exhausted.
        """
        step = self.state.steps[self.state.current_step]
        step_id = step["id"]
        step_dir = WORKSPACE_DIR / STEPS_DIR / step_id

        if os.getenv("SKIP_AI_ANALYSIS", "false").lower() == "true":
            ts_print(f"[ai_analysis] SKIP_AI_ANALYSIS=true, skipping for step {step_id}")
            ascend_head = run_git(self.state.vllm_ascend_path, "rev-parse", "HEAD").strip()
            self.state.cur_vllm_commit = step["end_commit"]
            self.state.cur_ascend_commit = ascend_head
            self.state.cur_patch_path = ""
            return True

        previous_step = self.state.steps[self.state.current_step - 1] if self.state.current_step > 0 else None
        previous_step_id = previous_step["id"] if previous_step else ""
        previous_step_summary_path = (
            WORKSPACE_DIR / STEPS_DIR / previous_step_id / EACH_STEP_SUMMARY_FILE
            if previous_step_id else ""
        )
        is_last_step = self.state.current_step == self.state.total_steps - 1

        vllm_path = self.state.vllm_path
        ascend_path = self.state.vllm_ascend_path

        if self.state.retry_count == 0:
            run_git(vllm_path, "checkout", step["end_commit"])
            ts_print(f"[ai_analysis] {step_id}: vllm checked out to {step['end_commit'][:8]}")

            try:
                ref_result = run_update(
                    ascend_path=Path(ascend_path),
                    old_commit=step["start_commit"],
                    new_commit=step["end_commit"],
                )
                ts_print(f"[ai_analysis] {step_id}: updated commit ref in "
                      f"{len(ref_result['files_updated'])} file(s): "
                      f"{ref_result['files_updated']}")
            except ValueError:
                ts_print(f"[ai_analysis] {step_id}: commit ref already updated, skipping")

            # Pre-seed the cumulative summary so the agent only has to append.
            summary_path = step_dir / EACH_STEP_SUMMARY_FILE
            if (previous_step_summary_path and Path(previous_step_summary_path).exists()
                    and not summary_path.exists()):
                summary_path.write_text(
                    Path(previous_step_summary_path).read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
        else:
            ts_print(f"[ai_analysis] {step_id}: retry count {self.state.retry_count}, "
                     f"entering fix mode")

        error_logs: list[str] = list(self.state.test_errors)
        critic_failed = False
        critic_issues: list = []
        preci_failed = False
        patch_path = step_dir / VLLM_GIT_PATCH_FILE
        changed_files_path = step_dir / VLLM_GIT_CHANGED_FILES
        adapt_result: AdaptResult | None = None

        for attempt in range(1, 4):
            # fix_preci only after an actual pre-CI failure: a retry after an
            # agent failure must rerun the full task (and stay subject to the
            # liveness check, which fix_preci is exempt from).
            if self.state.test_errors or critic_failed:
                mode = "fix"
            elif preci_failed:
                mode = "fix_preci"
            else:
                mode = "adapt"
            attempt_tag = f"r{self.state.retry_count}-a{attempt}"
            ts_print(f"[ai_analysis] {step_id}: opencode attempt {attempt}, "
                     f"mode={mode}, tag={attempt_tag}")

            adapt_result = run_opencode_adapter({
                "step_id": step_id,
                "previous_step_id": previous_step_id,
                "previous_step_summary_path": str(previous_step_summary_path),
                "is_last_step": is_last_step,
                "step_dir": str(step_dir),
                "patch_path": str(patch_path),
                "changed_files_path": str(changed_files_path),
                "ascend_path": ascend_path,
                "release_tag": self.state.release_tag,
                "vllm_path": vllm_path,
                "reference_dir": _REFERENCE_DIR,
                "mode": mode,
                "error_logs": json.dumps(error_logs, ensure_ascii=False),
                "error_content": self._error_content(error_logs, critic_issues),
                "code_structure_guide_file": EACH_STEP_CODE_STRUCTURE_GUIDE_FILE,
                "attempt_tag": attempt_tag,
                "stale_mappings": self._stale_mappings() if is_last_step else "",
            })

            if adapt_result.agent_failed:
                ts_print(f"[ai_analysis] {step_id}: opencode failed: "
                         f"{adapt_result.failure_reason}")
                if attempt == 3:
                    raise StepFailure(f"opencode failed: {adapt_result.failure_reason}")
                continue

            # Liveness: an adapt/fix agent that produced no analysis never engaged.
            if mode != "fix_preci" and not (step_dir / "analysis.md").exists():
                ts_print(f"[ai_analysis] {step_id}: agent produced no analysis artifact")
                if attempt == 3:
                    raise StepFailure("agent produced no analysis artifact")
                continue

            git_intent_to_add(ascend_path)
            check_result = run_check(ascend_path, self.state.release_tag, vllm_path=vllm_path)
            if not check_result["all_passed"]:
                log_path = step_dir / PRE_CI_CHECK_FILE
                log_path.write_text(json.dumps(check_result, indent=2, ensure_ascii=False))
                if str(log_path) not in error_logs:
                    error_logs.append(str(log_path))
                preci_failed = True
                critic_failed = False
                critic_issues = []
                ts_print(f"[ai_analysis] {step_id}: pre_ci failed → {log_path}")
                continue

            ts_print(f"[ai_analysis] {step_id}: pre_ci passed on attempt {attempt}")

            if os.getenv("MAIN2MAIN_CRITIC", "true").lower() != "false":
                review = run_opencode_review({
                    "step_id": step_id,
                    "ascend_path": ascend_path,
                    "vllm_path": vllm_path,
                    "patch_path": str(patch_path),
                    "step_dir": str(step_dir),
                    "release_tag": self.state.release_tag,
                    "diff_excerpt": run_git(ascend_path, "diff", "HEAD")[:60000],
                    "checklist": _review_checklist(),
                    "attempt_tag": attempt_tag,
                })
                if review["agent_failed"]:
                    ts_print(f"[ai_analysis] {step_id}: critic agent failed — "
                             f"accepting the adaptation without review")
                elif review["verdict"] == "fail":
                    critic_failed = True
                    critic_issues = review["issues"]
                    review_log = step_dir / EACH_STEP_REVIEW_FILE
                    if str(review_log) not in error_logs:
                        error_logs.append(str(review_log))
                    ts_print(f"[ai_analysis] {step_id}: critic rejected with "
                             f"{len(critic_issues)} issue(s)")
                    if attempt == 3:
                        raise StepFailure("critic rejected the adaptation")
                    continue
                else:
                    ts_print(f"[ai_analysis] {step_id}: critic passed")
            break
        else:
            raise StepFailure("pre-CI failed after 3 attempts")

        self.state.test_errors = []

        summary_path = step_dir / EACH_STEP_SUMMARY_FILE
        if adapt_result and adapt_result.step_summary and not summary_path.exists():
            summary_path.write_text(adapt_result.step_summary, encoding="utf-8")

        adaptation_patch_path = step_dir / EACH_STEP_TARGET_PATCH_FILE
        # intent-to-add was applied before run_check, so new files are included
        adaptation_patch = run_git(ascend_path, "diff", "HEAD")
        adaptation_patch_path.write_text(adaptation_patch, encoding="utf-8")

        changed_files = run_git(ascend_path, "diff", "--name-only", "HEAD").strip().splitlines()
        changed_files = [f for f in changed_files if f]  # filter empty lines

        ascend_head = run_git(ascend_path, "rev-parse", "HEAD").strip()

        self.state.cur_vllm_commit = step["end_commit"]
        self.state.cur_ascend_commit = ascend_head
        self.state.cur_patch_path = str(adaptation_patch_path)
        self.state.changed_files = changed_files

        ts_print(f"[ai_analysis] {step_id}: done, "
              f"is_noop={adapt_result.is_noop}, "
              f"status={adapt_result.status!r}, "
              f"modified={adapt_result.modified_files}, "
              f"vllm={step['end_commit'][:8]}, ascend={ascend_head[:8]}")

        # Reset any accidental changes to vllm (opencode should only touch
        # vllm-ascend, but may sometimes modify vllm).  Dirty vllm breaks the
        # next step's git checkout.
        reset_r = subprocess.run(
            ["git", "checkout", "--", "."],
            cwd=vllm_path, capture_output=True, text=True,
        )
        if reset_r.returncode != 0:
            ts_print(f"[ai_analysis] {step_id}: failed to reset vllm: {reset_r.stderr.strip()}")

        return True

    def _error_content(self, error_logs: list[str], critic_issues: list) -> str:
        """Inline-able error context: critic issues + error-log JSON, most
        recent first, capped at 8000 chars."""
        parts: list[str] = []
        if critic_issues:
            parts.append(json.dumps({"critic_issues": critic_issues},
                                    ensure_ascii=False, indent=1))
        for path in reversed(error_logs):
            try:
                parsed = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            parts.append(json.dumps(parsed, ensure_ascii=False, indent=1))
        return "\n".join(parts)[:8000]

    def _stale_mappings(self) -> str:
        """Upstream paths from the File Mapping Table that no longer exist in
        the vllm tree (checked out at the step's end commit)."""
        guide = Path(_REFERENCE_DIR) / "code-structure-guide.md"
        if not guide.exists():
            return ""
        text = guide.read_text(encoding="utf-8")
        match = re.search(
            r"<!-- BEGIN REFERENCE: file-mapping -->(.*?)<!-- END REFERENCE: file-mapping -->",
            text, re.DOTALL,
        )
        if not match:
            return ""
        vllm_root = Path(self.state.vllm_path)
        missing: list[str] = []
        for line in match.group(1).splitlines():
            if not line.strip().startswith("|"):
                continue
            first_col = line.split("|")[1]
            for upstream in re.findall(r"`([^`]+)`", first_col):
                if not upstream.startswith("vllm/"):
                    continue
                if "*" in upstream:
                    if not glob.glob(str(vllm_root / upstream)):
                        missing.append(upstream)
                elif not (vllm_root / upstream).exists():
                    missing.append(upstream)
        return "\n".join(missing)

    def _record_fix_candidate(self):
        """Stage the error→fix pair of a failed→fixed round for human curation
        into error-pattern-examples.md."""
        step = self.state.steps[self.state.current_step]
        path = Path(__file__).parent / "reference" / REFERENCE_CANDIDATES_FILE
        new_files = sorted(set(self.state.changed_files) - set(self.state.prev_step_files))
        entry = (f"## {step['id']} {step['start_commit'][:8]}..{step['end_commit'][:8]} "
                 f"(fixed after {self.state.retry_count} retries)\n\n")
        if self.state.last_error_excerpt:
            entry += self.state.last_error_excerpt + "\n\n"
        entry += ("Files changed in this step:\n"
                  + "".join(f"- {f}\n" for f in new_files)
                  + "\n---\n")
        try:
            if not path.exists():
                path.write_text(
                    "# Error-pattern candidates\n\n"
                    "Entries below are appended automatically after a failed→fixed "
                    "round.\nCurate the useful ones into error-pattern-examples.md "
                    "and delete.\n\n",
                    encoding="utf-8",
                )
            with path.open("a", encoding="utf-8") as fh:
                fh.write(entry)
            ts_print(f"[process_steps] recorded fix candidate for {step['id']} → {path}")
        except OSError as exc:
            ts_print(f"[process_steps] could not record fix candidate: {exc!r}")

    def _run_e2e_test(self):
        step = self.state.steps[self.state.current_step]
        step_id = step["id"]
        ts_print(f"run_e2e_test: {step_id} round={self.state.retry_count}")

        if os.getenv("SKIP_E2E_TEST", "false").lower() == "true":
            ts_print("[run_e2e_test] SKIP_E2E_TEST=true, treating as passed")
            return True

        remote = os.getenv("MAIN2MAIN_RUN_TESTS_REMOTE") or (
            "env" if os.getenv("MAIN2MAIN_REMOTE_HOST") and os.getenv("MAIN2MAIN_REMOTE_CONTAINER")
            else None
        )

        ts_print(f"The adaptation patch is at: {self.state.cur_patch_path}")
        result = run_tests(
            vllm_path=self.state.vllm_path,
            vllm_commit=self.state.cur_vllm_commit,
            ascend_path=self.state.vllm_ascend_path,
            ascend_commit=self.state.cur_ascend_commit,
            patch_path=self.state.cur_patch_path or None,
            step_id=step_id,
            select_by_files=self.state.changed_files or None,
            test_cases=_parse_test_cases_env(),
            remote=remote,
            in_place=not remote,
            round_number=self.state.retry_count,
            log_dir=str(WORKSPACE_DIR / STEPS_DIR),
        )

        test_passed = result.get("can_commit", False)
        ts_print(f"test_passed={test_passed}, ci_result={result.get('ci_result')}")

        if result.get("tests_skipped"):
            ts_print(f"[run_e2e_test] WARNING: no tests selected — step {step_id} "
                     f"passes WITHOUT runtime validation; set MAIN2MAIN_SMOKE_TESTS "
                     f"to put a floor under the signal.")

        if not test_passed:
            self.state.test_errors = [result["result_path"]]
            excerpt = {k: result.get(k)
                       for k in ("ci_result", "can_commit", "suite_results")
                       if k in result}
            self.state.last_error_excerpt = json.dumps(
                excerpt, ensure_ascii=False, default=str
            )[:2000]

        return test_passed

    @listen(process_steps)
    def generate_final_post(self):
        completed = self.state.final_status == UpgradeCompleted

        if self.state.current_step == 0:
            ts_print("[generate_final_post] upgrade failed, no step succeeded")
            (WORKSPACE_DIR / FINAL_SUMMARY_FILE).write_text(
                "main2main adaptation failed — no steps completed.\n", encoding="utf-8"
            )
        else:
            # The last successful step's patch is cumulative: git diff HEAD after
            # all successful adaptations.
            last_step = self.state.steps[self.state.current_step - 1]
            step_dir = WORKSPACE_DIR / STEPS_DIR / last_step["id"]
            step_patch = step_dir / EACH_STEP_TARGET_PATCH_FILE
            if step_patch.exists():
                shutil.copy2(step_patch, WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE)

            # Build PR body: per-file list with description + upstream commit
            # links.  Step patches are cumulative, so attribute a file to a step
            # only when its diff section is new or changed vs the previous step.
            commit_url = "https://github.com/vllm-project/vllm/commit"
            file_to_commits: dict[str, list[str]] = {}
            file_descs: dict[str, list[str]] = {}
            prev_patch_text = ""
            for i in range(self.state.current_step):
                s = self.state.steps[i]
                sp = WORKSPACE_DIR / STEPS_DIR / s["id"] / EACH_STEP_TARGET_PATCH_FILE
                cur_patch_text = sp.read_text(encoding="utf-8") if sp.exists() else ""
                touched = _files_touched_between(prev_patch_text, cur_patch_text)
                prev_patch_text = cur_patch_text
                for fname in sorted(touched):
                    if fname == TRACKING_FILE:
                        continue
                    file_to_commits.setdefault(fname, []).append(s["end_commit"][:8])
                    # Extract Cause/Change lines from the step's AI summary
                    ssp = WORKSPACE_DIR / STEPS_DIR / s["id"] / EACH_STEP_SUMMARY_FILE
                    if ssp.exists() and fname not in file_descs:
                        desc_lines = []
                        for dline in ssp.read_text(encoding="utf-8").strip().splitlines():
                            dl = dline.strip()
                            if dl.startswith("Cause:") or dl.startswith("Change:"):
                                desc_lines.append(dl)
                        if desc_lines:
                            file_descs[fname] = desc_lines

            reached_for_header = (
                (self.state.target_commit or self.state.cur_vllm_commit) if completed
                else self.state.last_verified_vllm_commit
            )
            parts = [
                "### What this PR does / why we need it?",
                "",
                f"vllm upstream `{self.state.base_commit[:8]}...{reached_for_header[:8]}` "
                f"({self.state.current_step}/{self.state.total_steps} steps).",
                "",
            ]
            if file_to_commits:
                for fname in sorted(file_to_commits):
                    parts.append(f"#### {fname}")
                    parts.append("")
                    descs = file_descs.get(fname, [])
                    for d in descs:
                        parts.append(f"- {d}")
                    for vc in file_to_commits[fname]:
                        parts.append(f"- Upstream source: [{vc}]({commit_url}/{vc})")
                    parts.append("")
            else:
                parts.append("No vllm-ascend changes needed.")
                parts.append("")

            (WORKSPACE_DIR / FINAL_SUMMARY_FILE).write_text("\n".join(parts), encoding="utf-8")

            last_guide_path = step_dir / EACH_STEP_CODE_STRUCTURE_GUIDE_FILE
            if last_guide_path.exists():
                shutil.copy2(last_guide_path, WORKSPACE_DIR / FINAL_CODE_STRUCTURE_GUIDE_FILE)
                ts_print("[generate_final_post] Copied code-structure-guide to workspace.")

        status = "completed" if completed else "failed"
        status_json = {
            "status": status,
            "steps_completed": self.state.current_step,
            "steps_total": self.state.total_steps,
            "reached_commit": self.state.last_verified_vllm_commit,
            "old_commit": self.state.base_commit,
            "new_commit": self.state.target_commit or self.state.cur_vllm_commit,
            "failure_reason": self.state.failure_reason,
            "model": os.getenv("MAIN2MAIN_MODEL", ""),
        }
        (WORKSPACE_DIR / "final_status.json").write_text(
            json.dumps(status_json, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        ledger_entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "base": self.state.base_commit,
            "target": self.state.target_commit or self.state.cur_vllm_commit,
            "status": status,
            "steps_completed": self.state.current_step,
            "steps_total": self.state.total_steps,
            "last_verified": self.state.last_verified_vllm_commit,
            "failure_reason": self.state.failure_reason,
            "model": os.getenv("MAIN2MAIN_MODEL", ""),
        }
        with (PROJECT_ROOT / RUNS_LEDGER_FILE).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(ledger_entry, ensure_ascii=False) + "\n")

        if os.getenv("MAIN2MAIN_KEEP_BRANCH", "false").lower() != "true":
            # Restoration must never mask the run result: status json is already
            # written, so log failures instead of raising.
            try:
                run_git(self.state.vllm_path, "checkout", self.state.original_vllm_ref)
                ts_print(f"[generate_final_post] Restored vllm to '{self.state.original_vllm_ref}'.")
                # git reset clears intent-to-add entries checkout -f won't touch
                run_git(self.state.vllm_ascend_path, "reset")
                # Agent-created files stay behind untracked after reset; delete the
                # ones final_target.patch will recreate, or push_to_github's
                # `git apply` fails with "already exists in working directory".
                final_patch = WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE
                if final_patch.exists():
                    for rel in _patch_new_files(final_patch.read_text(encoding="utf-8")):
                        (Path(self.state.vllm_ascend_path) / rel).unlink(missing_ok=True)
                run_git(self.state.vllm_ascend_path, "checkout", "-f", self.state.original_ascend_ref)
                ts_print(f"[generate_final_post] Restored vllm-ascend to '{self.state.original_ascend_ref}'.")
            except Exception as exc:
                ts_print(f"[generate_final_post] restoration failed: {exc!r} — "
                         f"inspect the repos manually")

    @listen(generate_final_post)
    def push_to_github(self):
        if os.getenv("PUSH_TO_GITHUB", "false").lower() != "true":
            ts_print("[push] PUSH_TO_GITHUB is not true, skipping.")
            return "SKIP_PUSH"

        github_repo = os.getenv("GITHUB_REPO", "")
        if not github_repo:
            ts_print("[push] GITHUB_REPO is empty, cannot create PR.")
            return "SKIP_PUSH"

        run_failed = self.state.final_status != UpgradeCompleted
        if run_failed and os.getenv("MAIN2MAIN_KEEP_BRANCH", "false").lower() == "true":
            ts_print("[push] REFUSING to push: run failed and MAIN2MAIN_KEEP_BRANCH=true — "
                     "the kept tree may contain unverified failed-step changes.")
            return "SKIP_PUSH"

        head_fork = os.getenv("HEAD_FORK", "")
        draft = os.getenv("PR_DRAFT", "true").lower() == "true"
        labels_str = os.getenv("PR_LABELS", "ready")
        labels = [lbl.strip() for lbl in labels_str.split(",") if lbl.strip()]
        branch_name = os.getenv("PR_BRANCH_NAME", "")

        partial = (self.state.current_step, self.state.total_steps) if run_failed else None
        new_commit = (self.state.last_verified_vllm_commit if partial
                      else (self.state.target_commit or self.state.cur_vllm_commit))

        return push_and_create_pr(
            ascend_path=Path(self.state.vllm_ascend_path),
            github_repo=github_repo,
            patch_path=WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE,
            summary_path=WORKSPACE_DIR / FINAL_SUMMARY_FILE,
            old_commit=self.state.base_commit,
            new_commit=new_commit,
            head_fork=head_fork,
            draft=draft,
            labels=labels,
            branch_name=branch_name,
            partial=partial,
            run_status=("failed" if run_failed else "completed"),
        )
