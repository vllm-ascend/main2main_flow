
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from crewai.flow import Flow, listen, start, router

from main2main_flow.scripts.agent.opencode_adapter import AdaptResult, run_opencode_adapter, run_opencode_review
from main2main_flow.scripts.utils.detect_commits import detect
from main2main_flow.scripts.utils.plan_steps import run_plan
from main2main_flow.scripts.utils.pre_ci_check import run_check
from main2main_flow.scripts.utils.push_to_github import push_and_create_pr
from main2main_flow.scripts.utils.run_tests import run_tests
from main2main_flow.scripts.utils.update_commit_reference import run_update
from main2main_flow.scripts.utils.utils import (
    UpgradeCompleted, UpgradeFailed,
    HasCommit, HasNoCommit, resolve_path, WORKSPACE_DIR, DETECT_FILE, STEPS_FILE, FINAL_SUMMARY_FILE, FINAL_TARGET_PATCH_FILE,
    STEPS_DIR, VLLM_GIT_PATCH_FILE, VLLM_GIT_CHANGED_FILES, PRE_CI_CHECK_FILE,
    EACH_STEP_SUMMARY_FILE, EACH_STEP_TARGET_PATCH_FILE, EACH_STEP_CODE_STRUCTURE_GUIDE_FILE,
    FINAL_CODE_STRUCTURE_GUIDE_FILE, run_git, ts_print
)

def _resolve_test_cases() -> list[str] | None:
    """Merge test cases from env, allowlist, and blocklist.

    Returns the final deduplicated list, or None to fall back to
    file-based auto-selection.
    """
    tests: list[str] = []
    # Env-provided test cases (MAIN2MAIN_TEST_CASES in CI workflow)
    env_val = os.getenv("MAIN2MAIN_TEST_CASES", "").strip()
    if env_val:
        tests.extend(t.strip() for t in env_val.replace("\n", " ").split() if t.strip())

    # Test policy: allowlist (always include) and blocklist (always exclude)
    policy_path = Path(__file__).parent / "test_policy.json"
    blocked: set[str] = set()
    if policy_path.exists():
        try:
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            for t in policy.get("allowlist", []):
                if isinstance(t, str) and t.strip():
                    tests.append(t.strip())
            for t in policy.get("blocklist", []):
                if isinstance(t, str) and t.strip():
                    blocked.add(t.strip())
        except (json.JSONDecodeError, KeyError):
            ts_print("[test_policy] failed to parse test_policy.json, ignoring")

    # Deduplicate and apply blocklist
    seen: set[str] = set()
    result: list[str] = []
    for t in tests:
        if t in seen or t in blocked:
            continue
        seen.add(t)
        result.append(t)

    return result or None


class Main2MainState(BaseModel):
    vllm_path: str = ""
    vllm_ascend_path: str = ""
    target_commit: str = ""
    test_log_dir: str = ""

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

    # Tracked from detect step for PR title / push
    base_commit: str = ""

    # Changed files from current adaptation step (for precise test selection)
    changed_files: list[str] = []

    # Persistent opencode session ID for full conversational context
    session_id: str = ""

    # Persistent QA session — reused across retries so the reviewer doesn't
    # re-read the entire codebase from scratch on every attempt.
    qa_session_id: str = ""

    # Last vllm commit that actually passed e2e tests (not just was adapted)
    last_verified_commit: str = ""


def _run_adapter_qa(
    ascend_path: str, vllm_path: str, step_id: str,
    step_dir: str, release_tag: str,
    upstream_patch_path: str = "",
    qa_session_id: str = "",
) -> tuple[list[str], str]:
    """adapter-qa: independent review of the current diff.

    Fresh opencode session (no --session reuse) — the reviewer sees only the
    diff and the review-lessons checklist, without the generator's context.
    Generator/critic separation.
    """
    diff = subprocess.run(
        ["git", "diff", "HEAD"], cwd=ascend_path,
        capture_output=True, text=True,
    ).stdout.strip()
    if not diff:
        return [], qa_session_id  # no changes to review

    lessons_path = Path(__file__).parent / "agents" / "adapter-qa" / "reference" / "review-lessons.md"
    if not lessons_path.exists():
        return [], qa_session_id
    # Load adapter-qa prompt template
    qa_template_path = Path(__file__).parent / "agents" / "adapter-qa" / "SKILL.md"
    qa_template = ""
    if qa_template_path.exists():
        qa_template = qa_template_path.read_text(encoding="utf-8")

    # Extract §9 checklist from review-lessons.md
    lessons = lessons_path.read_text(encoding="utf-8")
    idx = lessons.find("## 9.")
    checklist = lessons[idx:] if idx != -1 else lessons

    # Truncate diff if it's huge
    diff_limit = 8000
    diff_snippet = diff if len(diff) <= diff_limit else diff[:diff_limit] + "\n... [truncated]"

    review_path = str(Path(step_dir) / "review.json")
    if qa_template:
        upstream_patch = ""
        if upstream_patch_path:
            pp = Path(upstream_patch_path)
            if pp.exists():
                upstream_patch = pp.read_text(encoding="utf-8")[:4000]

        prompt = qa_template.format(
            step_id=step_id,
            release_tag=release_tag,
            vllm_path=vllm_path,
            ascend_path=ascend_path,
            patch_path=upstream_patch,
            review_path=review_path,
            diff_content=diff_snippet,
            review_checklist=checklist,
        )
    else:
        # Fallback inline prompt
        prompt = f"""You are a code reviewer. Review the following adaptation diff for policy violations.
Return ONLY a JSON object: {{"verdict": "pass"|"fail", "issues": [...]}}.
DIFF:\n{diff_snippet}\nVERDICT (JSON only):"""

    model = os.environ.get("MAIN2MAIN_MODEL_REVIEW") or os.environ.get("MAIN2MAIN_MODEL", "deepseek/deepseek-chat")

    ts_print(f"[adapter-qa] {step_id}: running review (model={model}, diff={len(diff)} bytes) ...")
    qa_log = Path(step_dir) / "opencode_qa.log"
    qa_raw = Path(step_dir) / "opencode_qa_raw.jsonl"
    qa_stderr = Path(step_dir) / "opencode_qa_stderr.log"
    output_text, new_session_id = run_opencode_review(
        prompt, log_path=qa_log, raw_path=qa_raw, stderr_path=qa_stderr,
        session_id=qa_session_id, model=model,
    )
    if not output_text.strip():
        ts_print(f"[adapter-qa] {step_id}: opencode produced no output")
        return ["critic: opencode produced no output"], new_session_id

    # Read the verdict from review.json (written by the model per SKILL.md to the step dir)
    import json as _json
    review_json = Path(review_path)
    if review_json.exists():
        try:
            review = _json.loads(review_json.read_text(encoding="utf-8"))
            verdict = review.get("verdict", "")
            issues = review.get("issues", [])
            if verdict == "pass":
                ts_print(f"[adapter-qa] {step_id}: pass")
                return [], new_session_id
            ts_print(f"[adapter-qa] {step_id}: fail — {len(issues)} issue(s)")
            return ([f"{i.get('file', '?')}:{i.get('line', '?')}: {i.get('issue', '?')}" for i in issues],
                    new_session_id)
        except (_json.JSONDecodeError, KeyError):
            ts_print(f"[adapter-qa] {step_id}: fail (review.json unparseable)")
            return ["critic: review.json could not be parsed"], new_session_id

    ts_print(f"[adapter-qa] {step_id}: fail (no review.json found)")
    return ["critic: no review.json found — opencode did not produce expected output"], new_session_id


class Main2MainFlow(Flow[Main2MainState]):

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

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
        if not self.state.test_log_dir:
            self.state.test_log_dir = str(WORKSPACE_DIR / "test-logs")

        vllm_branch = run_git(self.state.vllm_path, "branch", "--show-current").strip()
        self.state.original_vllm_ref = vllm_branch or run_git(self.state.vllm_path, "rev-parse", "HEAD").strip()
        ascend_branch = run_git(self.state.vllm_ascend_path, "branch", "--show-current").strip()
        self.state.original_ascend_ref = ascend_branch or run_git(self.state.vllm_ascend_path, "rev-parse", "HEAD").strip()

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

        # generate steps.json in workspace
        plan = run_plan(vllm_path, result["base_commit"], result["target_commit"])
        self.state.steps = plan["steps"]
        self.state.total_steps = len(plan["steps"])

        (WORKSPACE_DIR / STEPS_FILE).write_text(
            json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        if self.state.total_steps == 0:
            return HasNoCommit

        ts_print(f"[analyze] planned {self.state.total_steps} step(s) covering "
              f"{plan['total_commits']} commit(s).")
        # Print step plan without upstream_patch (it's verbose and already on disk)
        steps_preview = []
        for s in plan["steps"]:
            sp = dict(s)
            sp.pop("upstream_patch", None)
            steps_preview.append(sp)
        ts_print(json.dumps(steps_preview, indent=2, ensure_ascii=False))

        # generate every step folder in workspace
        for index in range(self.state.total_steps):
            step = self.state.steps[index]
            step_dir = WORKSPACE_DIR / STEPS_DIR / step["id"]
            step_dir.mkdir(parents=True, exist_ok=True)
            (step_dir / VLLM_GIT_PATCH_FILE).write_text(step["upstream_patch"], encoding="utf-8")
            (step_dir / VLLM_GIT_CHANGED_FILES).write_text(step["changed_files"], encoding="utf-8")

        return HasCommit

    @listen(HasNoCommit)
    def has_no_commit(self):
        ts_print(f"[done] 仓库已同步，无需适配，流程结束。")

    @listen(HasCommit)
    def process_steps(self):
        ascend_path = self.state.vllm_ascend_path
        while self.state.current_step < self.state.total_steps:
            step = self.state.steps[self.state.current_step]
            step_id = step["id"]

            if not self._ai_analysis():
                # Adaptation could not pass pre_ci + critic after 3 attempts.
                # Discard the broken working-tree changes and fall through to
                # generate_final_post / push with whatever passed in prior steps.
                ts_print(f"[process_steps] {step_id}: ai_analysis exhausted retries, "
                         f"reverting to last committed state")
                run_git(ascend_path, "checkout", "--", ".")
                subprocess.run(["git", "clean", "-fd"],
                               cwd=ascend_path, capture_output=True)
                self.state.final_status = UpgradeFailed
                return

            test_pass = self._run_e2e_test()
            if test_pass:
                # Commit the successful adaptations so they survive a future
                # step failure and become part of the baseline for the next run.
                run_git(ascend_path, "add", "-A")
                subprocess.run(["git", "commit", "-s", "-m",
                                f"main2main: step {step_id} ({step['end_commit'][:8]})"],
                               cwd=ascend_path, capture_output=True)
                self.state.current_step += 1
                self.state.retry_count = 0
                self.state.last_verified_commit = self.state.cur_vllm_commit
                continue
            else:
                self.state.retry_count += 1
                if self.state.retry_count >= 3:
                    self.state.final_status = UpgradeFailed
                    return
                continue
        self.state.final_status = UpgradeCompleted

    def _ai_analysis(self) -> bool:
        step = self.state.steps[self.state.current_step]
        step_id = step["id"]
        step_dir = WORKSPACE_DIR / STEPS_DIR / step_id

        # Skip opencode adaptation when upstream_patch is empty (no vllm/ code
        # changes in this step).  Still advance verified.commit and set state
        # so e2e tests can verify the new vllm commit doesn't break anything.
        upstream_patch = step.get("upstream_patch", "")
        if os.getenv("SKIP_AI_ANALYSIS", "false").lower() == "true" or not upstream_patch.strip():
            if not upstream_patch.strip():
                ts_print(f"[ai_analysis] {step_id}: no vllm/ code changes, skipping adaptation")
            else:
                ts_print(f"[ai_analysis] SKIP_AI_ANALYSIS=true, skipping for step {step_id}")

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

            # Capture working tree changes (verified.commit update) as patch
            subprocess.run(["git", "add", "-N", "."], cwd=ascend_path,
                           capture_output=True)
            adaptation_patch = run_git(ascend_path, "diff", "HEAD")
            (step_dir / EACH_STEP_TARGET_PATCH_FILE).write_text(
                adaptation_patch, encoding="utf-8")

            # Write minimal step summary
            summary_path = step_dir / EACH_STEP_SUMMARY_FILE
            if not summary_path.exists():
                summary_path.write_text(
                    f"- {step_id}: No vllm/ code changes, advanced verified.commit to "
                    f"{step['end_commit'][:8]}\n",
                    encoding="utf-8",
                )

            # Clean up review artifacts
            archive_dir = Path(ascend_path) / ".archive"
            if archive_dir.exists():
                shutil.rmtree(archive_dir)

            # Reset vllm working tree
            reset_r = subprocess.run(
                ["git", "checkout", "--", "."],
                cwd=vllm_path, capture_output=True, text=True,
            )
            if reset_r.returncode != 0:
                ts_print(f"[ai_analysis] {step_id}: failed to reset vllm: {reset_r.stderr.strip()}")

            ascend_head = run_git(ascend_path, "rev-parse", "HEAD").strip()
            self.state.cur_vllm_commit = step["end_commit"]
            self.state.cur_ascend_commit = ascend_head
            self.state.cur_patch_path = str(step_dir / EACH_STEP_TARGET_PATCH_FILE)
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
        else:
            ts_print(f"[ai_analysis] {step_id}: retry count {self.state.retry_count}, \
 skipping to fix mode")

        error_logs: list[str] = list(self.state.test_errors)
        patch_path = step_dir / VLLM_GIT_PATCH_FILE
        changed_files_path = step_dir / VLLM_GIT_CHANGED_FILES
        adapt_result: AdaptResult | None = None

        pre_ci_passed = False
        review_passed = False

        for attempt in range(1, 4):
            role = "adapter-fix" if error_logs else "adapter"
            ts_print(f"[ai_analysis] {step_id}: opencode attempt {attempt}, role={role}")
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
                "role": role,
                "error_logs": json.dumps(error_logs, ensure_ascii=False),
                "code_structure_guide_file": EACH_STEP_CODE_STRUCTURE_GUIDE_FILE,
                "mode": role,
            }, session_id=self.state.session_id)
            if adapt_result.session_id:
                self.state.session_id = adapt_result.session_id

            # pre_ci: mechanical checks (version, format, imports, temp files)
            check_result = run_check(ascend_path, self.state.release_tag, vllm_path=vllm_path)
            pre_ci_passed = check_result["all_passed"]
            if not pre_ci_passed:
                log_path = step_dir / PRE_CI_CHECK_FILE
                log_path.write_text(json.dumps(check_result, indent=2, ensure_ascii=False))
                error_logs = [str(log_path)]
                failures = []
                for check in check_result.get("checks", []):
                    if not check["passed"]:
                        st = "SKIPPED" if check.get("skipped", False) else "FAILED"
                        failures.append(f"{check['name']}: {st} — {check.get('detail', '')}")
                ts_print(f"[ai_analysis] {step_id}: pre_ci FAILED ({len(failures)} check(s)):")
                for f in failures:
                    ts_print(f"  {f}")
            else:
                error_logs = []
                ts_print(f"[ai_analysis] {step_id}: pre_ci passed on attempt {attempt}")

            # adapter-qa: logic review — only when pre_ci passed.
            # If pre_ci found mechanical issues the attempt will retry anyway,
            # so reviewing broken code is wasted time.
            if pre_ci_passed:
                review_issues, new_qa_sid = _run_adapter_qa(
                    ascend_path=ascend_path,
                    vllm_path=vllm_path,
                    step_id=step_id,
                    step_dir=str(step_dir),
                    release_tag=self.state.release_tag,
                    upstream_patch_path=str(step_dir / VLLM_GIT_PATCH_FILE),
                    qa_session_id=self.state.qa_session_id,
                )
                if new_qa_sid:
                    self.state.qa_session_id = new_qa_sid
                review_passed = not review_issues
            else:
                review_issues = []
                review_passed = False
                ts_print(f"[ai_analysis] {step_id}: critic skipped (pre_ci failed)")
            if review_issues:
                review_path = step_dir / "adapter-qa.md"
                review_path.write_text("\n".join(review_issues), encoding="utf-8")
                ts_print(f"[ai_analysis] {step_id}: critic found {len(review_issues)} issue(s) → {review_path}")
                if error_logs:
                    error_logs.append(str(review_path))
                else:
                    error_logs = [str(review_path)]
            else:
                ts_print(f"[ai_analysis] {step_id}: critic passed")

            if pre_ci_passed and review_passed:
                break

        if not (pre_ci_passed and review_passed):
            ts_print(f"[ai_analysis] {step_id}: FAILED after 3 attempts — skipping e2e")
            self.state.test_errors = error_logs if error_logs else []
            return False

        self.state.test_errors = []

        summary_path = step_dir / EACH_STEP_SUMMARY_FILE
        if adapt_result and adapt_result.step_summary and not summary_path.exists():
            summary_path.write_text(adapt_result.step_summary, encoding="utf-8")

        # Clean up review artifacts (e.g. .archive/review.json) that opencode
        # may have left behind during adapter-qa — these should not be committed.
        archive_dir = Path(ascend_path) / ".archive"
        if archive_dir.exists():
            shutil.rmtree(archive_dir)
            ts_print(f"[ai_analysis] {step_id}: removed .archive/ (review artifact)")

        adaptation_patch_path = step_dir / EACH_STEP_TARGET_PATCH_FILE
        # git diff HEAD excludes untracked files — run git add -N first
        # so new files created by the adaptation appear in the patch.
        subprocess.run(["git", "add", "-N", "."], cwd=ascend_path,
                       capture_output=True)
        # Verify the working tree includes format fixes (if any)
        diff_stat = subprocess.run(
            ["git", "diff", "--stat", "HEAD"], cwd=ascend_path,
            capture_output=True, text=True,
        ).stdout.strip()
        if diff_stat:
            ts_print(f"[ai_analysis] {step_id}: working tree diff before patch capture:\n{diff_stat[:500]}")
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
              f"is_noop={getattr(adapt_result, 'is_noop', False)}, "
              f"modified={getattr(adapt_result, 'modified_files', [])}, "
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

    def _run_e2e_test(self):
        step = self.state.steps[self.state.current_step]
        step_id = step["id"]
        ts_print(f"run_e2e_test: {step_id} round={self.state.retry_count}")

        if os.getenv("SKIP_E2E_TEST", "false").lower() == "true":
            ts_print(f"[run_e2e_test] SKIP_E2E_TEST=true, treating as passed")
            return True

        ts_print(f"The adaptation patch is at: {self.state.cur_patch_path}")
        result = run_tests(
            vllm_path=self.state.vllm_path,
            vllm_commit=self.state.cur_vllm_commit,
            ascend_path=self.state.vllm_ascend_path,
            ascend_commit=self.state.cur_ascend_commit,
            patch_path=self.state.cur_patch_path or None,
            step_id=step_id,
            select_by_files=self.state.changed_files or None,
            test_cases=_resolve_test_cases(),
            remote=os.getenv("MAIN2MAIN_RUN_TESTS_REMOTE") or None,
            round_number=self.state.retry_count,
            log_dir=str(WORKSPACE_DIR / STEPS_DIR),
        )

        test_passed = result.get("can_commit", False)
        # Write the e2e result dict to a known path so fix-mode error_logs
        # can reference it.  run_tests also writes this file, but the dict
        # is already returned — write it here so the path is predictable.
        tests_dir = WORKSPACE_DIR / STEPS_DIR / str(step_id) / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        summary_log = str(tests_dir / f"round-{self.state.retry_count}-result.json")
        import json as _json
        summary_log_path = Path(summary_log)
        if not summary_log_path.exists():
            summary_log_path.write_text(
                _json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        ts_print(f"test_passed={test_passed}, ci_result={result.get('ci_result')}")

        if not test_passed:
            # Collect per-test error details for fix mode: for each failed
            # test, read its -summary.json (structured code_bugs/env_flakes)
            # and the tail of its .log (raw traceback).  Both are inlined
            # so the agent sees the error directly without extra file ops.
            test_errors_detail = tests_dir / f"round-{self.state.retry_count}-test-errors.txt"
            detail_parts = []
            for test_name, tr in result.get("suite_results", {}).items():
                if tr.get("ci_result") in ("passed", "env_flake_pass"):
                    continue
                parts = [f"=== {test_name} ==="]
                # Structured summary (code_bugs/env_flakes with traceback)
                sp = Path(tr.get("summary_path", ""))
                if sp.exists():
                    try:
                        parts.append(f"[summary]\n{sp.read_text(encoding='utf-8')[:4000]}")
                    except Exception:
                        parts.append("[summary]\n(could not read)")
                # Raw log tail (full traceback, assertion details)
                lp = Path(tr.get("log_path", ""))
                if lp.exists():
                    try:
                        log_tail = lp.read_text(encoding='utf-8', errors='replace')
                        # Keep last 3000 chars — tracebacks are at the end
                        parts.append(f"[log tail]\n...\n{log_tail[-3000:]}")
                    except Exception:
                        parts.append("[log tail]\n(could not read)")
                detail_parts.append("\n\n".join(parts))
            if detail_parts:
                test_errors_detail.write_text("\n\n---\n\n".join(detail_parts), encoding="utf-8")
                self.state.test_errors = [str(test_errors_detail), summary_log]
            else:
                self.state.test_errors = [summary_log]

        return test_passed

    @listen(process_steps)
    def generate_final_post(self):
        # The last successful step's patch is cumulative: git diff HEAD after all
        # successful adaptations. Prefer its cumulative summary, and fall back to
        # concatenating available step summaries if the last one is missing.
        if self.state.current_step == 0:
            ts_print(f"[generate_final_post] fail to upgrade, no step success")
            (WORKSPACE_DIR / FINAL_SUMMARY_FILE).write_text(
                "main2main adaptation failed — no steps completed.\n", encoding="utf-8"
            )
            (WORKSPACE_DIR / "final_status.json").write_text(
                json.dumps({"status": "failed", "steps_completed": 0, "steps_total": self.state.total_steps,
                            "reached_commit": "", "old_commit": self.state.base_commit,
                            "new_commit": self.state.target_commit or ""}, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8"
            )
            return

        last_step = self.state.steps[self.state.current_step - 1]
        step_dir = WORKSPACE_DIR / STEPS_DIR / last_step["id"]
        final_summary_path = WORKSPACE_DIR / FINAL_SUMMARY_FILE
        step_patch = step_dir / EACH_STEP_TARGET_PATCH_FILE
        has_patch = step_patch.exists()
        if has_patch:
            shutil.copy2(step_patch, WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE)

        # Build PR body: concise numbered list matching PR #5595 style.
        # Each item: "Adapt <files> due to [commit](link) — <cause>"
        TRACKING_FILE = ".github/vllm-main-verified.commit"
        commit_url = "https://github.com/vllm-project/vllm/commit"

        # Collect per-step: adapted files, cause, and triggering commit
        step_items: list[dict] = []
        for i in range(self.state.current_step):
            s = self.state.steps[i]
            sp = WORKSPACE_DIR / STEPS_DIR / s["id"] / EACH_STEP_TARGET_PATCH_FILE
            if not sp.exists():
                continue
            files = []
            for line in sp.read_text(encoding="utf-8").splitlines():
                if line.startswith("diff --git a/"):
                    fname = line.split()[-1][2:]
                    if fname != TRACKING_FILE:
                        files.append(fname)
            if not files:
                continue
            cause = ""
            ssp = WORKSPACE_DIR / STEPS_DIR / s["id"] / EACH_STEP_SUMMARY_FILE
            if ssp.exists():
                for dline in ssp.read_text(encoding="utf-8").strip().splitlines():
                    dl = dline.strip()
                    if dl.startswith("Cause:"):
                        cause = dl.removeprefix("Cause:").strip()
                        break
            step_items.append({
                "files": files,
                "commit": s["end_commit"][:8],
                "cause": cause,
            })

        parts = [
            "### What this PR does / why we need it?",
            "",
            f"Upgrade vLLM commit to `{(self.state.target_commit or self.state.cur_vllm_commit)[:8]}`",
            "",
        ]
        for idx, item in enumerate(step_items, 1):
            files_str = ", ".join(f"`{f}`" for f in item["files"])
            commit_link = f"[{item['commit']}]({commit_url}/{item['commit']})"
            parts.append(f"{idx}. Adapt {files_str} due to {commit_link}")
            if item["cause"]:
                parts.append(f"   - {item['cause']}")
        parts.append("")

        final_summary_path.write_text("\n".join(parts), encoding="utf-8")

        status = "completed" if self.state.final_status == UpgradeCompleted else "failed"
        status_json = {
            "status": status,
            "steps_completed": self.state.current_step,
            "steps_total": self.state.total_steps,
            "reached_commit": self.state.last_verified_commit or self.state.cur_vllm_commit,
            "old_commit": self.state.base_commit,
            "new_commit": self.state.target_commit or self.state.cur_vllm_commit,
        }
        (WORKSPACE_DIR / "final_status.json").write_text(
            json.dumps(status_json, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        # Roll back .github/vllm-main-verified.commit to last_verified_commit
        # so the baseline ref (pushed by push_to_github.py) carries the
        # "verified up to here" marker, not the target commit.  Without this,
        # a failed run would leave the baseline pointing at an unverified
        # commit, and the next day's incremental run would skip adapting it.
        ascend_path = Path(self.state.vllm_ascend_path)
        verified_path = ascend_path / ".github" / "vllm-main-verified.commit"
        target = self.state.target_commit or self.state.cur_vllm_commit
        if (self.state.last_verified_commit
                and self.state.last_verified_commit != target
                and verified_path.exists()):
            verified_path.write_text(
                self.state.last_verified_commit + "\n", encoding="utf-8"
            )
            ts_print(f"[generate_final_post] Rolled back verified.commit to "
                     f"last_verified_commit={self.state.last_verified_commit[:8]}")

        # Ensure final_summary.md is non-empty (stub for dry runs)
        final = WORKSPACE_DIR / FINAL_SUMMARY_FILE
        if not final.exists() or final.stat().st_size == 0:
            final.write_text(
                f"main2main completed: {self.state.final_status}\n"
                f"Steps: {self.state.current_step}/{self.state.total_steps}\n",
                encoding="utf-8"
            )

        last_guide_path = step_dir / EACH_STEP_CODE_STRUCTURE_GUIDE_FILE
        if last_guide_path.exists():
            shutil.copy2(last_guide_path, WORKSPACE_DIR / FINAL_CODE_STRUCTURE_GUIDE_FILE)
            ts_print(f"[generate_final_post] Copied code-structure-guide to workspace.")

        if os.getenv("MAIN2MAIN_KEEP_BRANCH", "false").lower() != "true":
            vllm_path = self.state.vllm_path
            ascend_path = self.state.vllm_ascend_path
            run_git(vllm_path, "checkout", self.state.original_vllm_ref)
            ts_print(f"[generate_final_post] Restored vllm to '{self.state.original_vllm_ref}'.")
            run_git(ascend_path, "checkout", "-f", self.state.original_ascend_ref)
            ts_print(f"[generate_final_post] Restored vllm-ascend to '{self.state.original_ascend_ref}'.")

    @listen(generate_final_post)
    def push_to_github(self):
        if os.getenv("PUSH_TO_GITHUB", "false").lower() != "true":
            ts_print("[push] PUSH_TO_GITHUB is not true, skipping.")
            return "SKIP_PUSH"

        github_repo = os.getenv("GITHUB_REPO", "")
        if not github_repo:
            ts_print("[push] GITHUB_REPO is empty, cannot create PR.")
            return "SKIP_PUSH"

        head_fork = os.getenv("HEAD_FORK", "")
        draft = os.getenv("PR_DRAFT", "true").lower() == "true"
        labels_str = os.getenv("PR_LABELS", "ready")
        labels = [lbl.strip() for lbl in labels_str.split(",") if lbl.strip()]
        branch_name = os.getenv("PR_BRANCH_NAME", "")

        return push_and_create_pr(
            ascend_path=Path(self.state.vllm_ascend_path),
            github_repo=github_repo,
            patch_path=WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE,
            summary_path=WORKSPACE_DIR / FINAL_SUMMARY_FILE,
            old_commit=self.state.base_commit,
            new_commit=self.state.target_commit or self.state.cur_vllm_commit,
            head_fork=head_fork,
            draft=draft,
            labels=labels,
            branch_name=branch_name,
        )
