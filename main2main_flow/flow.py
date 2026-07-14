
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from crewai.flow import Flow, listen, start, router

from main2main_flow.scripts.agent.opencode_adapter import AdaptResult, run_opencode_adapter
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

def _parse_test_cases_env() -> list[str] | None:
    val = os.getenv("MAIN2MAIN_TEST_CASES", "").strip()
    if not val:
        return None
    return [t.strip() for t in val.replace("\n", " ").split() if t.strip()]


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

    # Last vllm commit that actually passed e2e tests (not just was adapted)
    last_verified_commit: str = ""


def _run_adapter_qa(
    ascend_path: str, vllm_path: str, step_id: str,
    step_dir: str, release_tag: str,
    upstream_patch_path: str = "",
) -> list[str]:
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
        return []  # no changes to review

    lessons_path = Path(__file__).parent / "agents" / "adapter-qa" / "reference" / "review-lessons.md"
    if not lessons_path.exists():
        return []
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
            diff_content=diff_snippet,
            review_checklist=checklist,
        )
    else:
        # Fallback inline prompt
        prompt = f"""You are a code reviewer. Review the following adaptation diff for policy violations.
Return ONLY a JSON object: {{"verdict": "pass"|"fail", "issues": [...]}}.
DIFF:\n{diff_snippet}\nVERDICT (JSON only):"""

    model = os.environ.get("MAIN2MAIN_MODEL_REVIEW") or os.environ.get("MAIN2MAIN_MODEL", "deepseek/deepseek-chat")

    auto_flag = "--dangerously-skip-permissions"
    help_r = subprocess.run(["opencode", "run", "--help"], capture_output=True, text=True)
    if "--auto" in (help_r.stdout + help_r.stderr):
        auto_flag = "--auto"

    ts_print(f"[adapter-qa] {step_id}: running review (model={model}, diff={len(diff)} bytes) ...")
    r = subprocess.run(
        ["opencode", "run", "--format", "json", "--model", model, auto_flag,
         "--", prompt],
        cwd=ascend_path, capture_output=True, text=True,
        timeout=300,  # 5 min for review
    )
    if r.returncode != 0:
        ts_print(f"[adapter-qa] {step_id}: opencode failed (exit {r.returncode})")
        return [f"Critic review crashed (exit {r.returncode}): {r.stderr.strip()[:500]}"]

    # Extract text from the last JSON event
    import json as _json
    text_parts = []
    for line in r.stdout.strip().splitlines():
        try:
            ev = _json.loads(line)
        except _json.JSONDecodeError:
            continue
        if ev.get("type") == "text":
            text_parts.append(ev.get("part", {}).get("text", ""))
    output = "\n".join(text_parts).strip()
    ts_print(f"[adapter-qa] {step_id}: output: {output[:500]}")

    # Try to parse JSON verdict from the output
    try:
        # Look for JSON block
        import re
        m = re.search(r'\{[^}]*"verdict"[^}]*\}', output)
        if m:
            verdict = _json.loads(m.group())
            if verdict.get("verdict") == "pass":
                return []
            return verdict.get("issues", ["critic: review flagged issues"])
    except _json.JSONDecodeError:
        pass

    # Fallback: if output mentions "pass" or is empty, assume OK
    if "pass" in output.lower() and "fail" not in output.lower():
        return []
    return ["critic: review output could not be parsed as pass — check critic_review.md"]


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
        while self.state.current_step < self.state.total_steps:
            if not self._ai_analysis():
                ts_print("[process_steps] ai_analysis exhausted retries, aborting")
                self.state.final_status = UpgradeFailed
                return
            test_pass = self._run_e2e_test()
            if test_pass:
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
        if os.getenv("SKIP_AI_ANALYSIS", "false").lower() == "true":
            step = self.state.steps[self.state.current_step]
            step_id = step["id"]
            step_dir = WORKSPACE_DIR / STEPS_DIR / step_id
            ts_print(f"[ai_analysis] SKIP_AI_ANALYSIS=true, skipping for step {step_id}")
            ascend_head = run_git(self.state.vllm_ascend_path, "rev-parse", "HEAD").strip()
            self.state.cur_vllm_commit = step["end_commit"]
            self.state.cur_ascend_commit = ascend_head
            self.state.cur_patch_path = str(step_dir / EACH_STEP_TARGET_PATCH_FILE)
            return True

        step = self.state.steps[self.state.current_step]
        step_id = step["id"]
        step_dir = WORKSPACE_DIR / STEPS_DIR / step_id
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

            # adapter-qa: logic review — runs regardless of pre_ci result
            review_issues = _run_adapter_qa(
                ascend_path=ascend_path,
                vllm_path=vllm_path,
                step_id=step_id,
                step_dir=str(step_dir),
                release_tag=self.state.release_tag,
                upstream_patch_path=str(step_dir / VLLM_GIT_PATCH_FILE),
            )
            review_passed = not review_issues
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

        # Post-patch diagnostic: run ruff-check on changed Python files
        py_files = [f for f in changed_files if f.endswith(".py")]
        if py_files:
            ruff_r = subprocess.run(
                ["ruff", "check"] + py_files, cwd=ascend_path,
                capture_output=True, text=True,
            )
            if ruff_r.returncode != 0:
                ts_print(f"[ai_analysis] {step_id}: ⚠ ruff-check found POST-PATCH issues in changed files:")
                for line in ruff_r.stdout.strip().splitlines()[:10]:
                    if line.strip():
                        ts_print(f"  {line.strip()}")
                ts_print(f"[ai_analysis] {step_id}: ↑ these will fail CI — run format.sh again!")

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
            test_cases=_parse_test_cases_env(),
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

        # Build PR body: per-file list with description + upstream commit links.
        # Extracts Cause/Change lines from the AI's step_summary, skipping verbose
        # "files checked but unchanged" lists and step header boilerplate.
        TRACKING_FILE = ".github/vllm-main-verified.commit"
        commit_url = "https://github.com/vllm-project/vllm/commit"
        file_to_commits: dict[str, list[str]] = {}
        file_descs: dict[str, list[str]] = {}
        for i in range(self.state.current_step):
            s = self.state.steps[i]
            sp = WORKSPACE_DIR / STEPS_DIR / s["id"] / EACH_STEP_TARGET_PATCH_FILE
            if sp.exists():
                pt = sp.read_text(encoding="utf-8")
                for line in pt.splitlines():
                    if line.startswith("diff --git a/"):
                        fname = line.split()[-1][2:]
                        if fname != TRACKING_FILE:
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

        parts = [
            "### What this PR does / why we need it?",
            "",
            f"vllm upstream `{self.state.base_commit[:8]}...{(self.state.target_commit or self.state.cur_vllm_commit)[:8]}` "
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

        # ---- pre-CI summary: only show non-OK results, with detail ----
        pre_ci_issues: list[str] = []
        for i in range(self.state.current_step):
            sd = WORKSPACE_DIR / STEPS_DIR / self.state.steps[i]["id"]
            pre_ci_path = sd / PRE_CI_CHECK_FILE
            if pre_ci_path.exists():
                try:
                    pci = json.loads(pre_ci_path.read_text(encoding="utf-8"))
                    for check in pci.get("checks", []):
                        if not check["passed"]:
                            detail = check.get("detail", "")
                            if check.get("skipped"):
                                pre_ci_issues.append(f"- **{check['name']}**: skipped — {detail}")
                            else:
                                pre_ci_issues.append(f"- **{check['name']}**: FAILED — {detail}")
                except Exception:
                    pass
        if pre_ci_issues:
            # Deduplicate
            seen = set()
            unique = []
            for item in pre_ci_issues:
                if item not in seen:
                    seen.add(item)
                    unique.append(item)
            parts.append("### Pre-CI Checks")
            parts.append("")
            parts.extend(unique)
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

