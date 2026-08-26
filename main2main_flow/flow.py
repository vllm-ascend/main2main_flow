
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from main2main_flow.scripts.agent.opencode_adapter import AdaptResult, run_opencode_adapter, run_opencode_review
from main2main_flow.scripts.utils.detect_commits import detect
from main2main_flow.scripts.utils.plan_steps import run_plan
from main2main_flow.scripts.utils.pre_ci_check import run_check
from main2main_flow.scripts.utils.lessons import (
    persist_lessons, submit_step_lesson, submit_gate_lesson)
from main2main_flow.scripts.utils.push_to_github import push_and_create_pr, resolve_squash_baseline
from main2main_flow.scripts.utils.run_tests import build_test_errors_detail
from main2main_flow.scripts.utils.e2e_dispatch import (
    E2EDispatchConfig, compute_test_groups, dispatch_prep,
    run_external_e2e)
from main2main_flow.scripts.utils.commit_ref import run_update
from main2main_flow.scripts.utils.final_quality_gate import run_final_quality_gate
from main2main_flow.scripts.utils.utils import (
    UpgradeCompleted, UpgradeFailed,
    HasCommit, HasNoCommit, resolve_path, WORKSPACE_DIR, DETECT_FILE, STEPS_FILE, FINAL_SUMMARY_FILE, FINAL_TARGET_PATCH_FILE,
    STEPS_DIR, VLLM_GIT_PATCH_FILE, VLLM_GIT_CHANGED_FILES, PRE_CI_CHECK_FILE,
    EACH_STEP_SUMMARY_FILE, EACH_STEP_TARGET_PATCH_FILE, EACH_STEP_CODE_STRUCTURE_GUIDE_FILE,
    FINAL_CODE_STRUCTURE_GUIDE_FILE, GENERATED_ARTIFACT_DIRS, run_git, ts_print
)

# Files that are tracking/metadata, not real adaptation changes — excluded
# from the PR description's file list.
_TRACKING_FILES_FOR_DESC: frozenset[str] = frozenset({
    ".github/vllm-main-verified.commit",
})


def _extract_diff_files(patch_text: str,
                        exclude: frozenset[str] = _TRACKING_FILES_FOR_DESC
                        ) -> list[str]:
    """Return de-duplicated file paths from a unified diff, in first-seen order.

    Parses ``diff --git a/<path> b/<path>`` headers.  Skips paths in *exclude*
    (default: tracking/metadata files).  Used by ``generate_final_post`` to
    read the accumulated patch's file list as the source of truth — per-step
    patches are incremental (last-retry-wins) and lose earlier retries' files.
    """
    files: list[str] = []
    seen: set[str] = set()
    for line in patch_text.splitlines():
        if not line.startswith("diff --git a/"):
            continue
        # Format: "diff --git a/<path> b/<path>"
        parts = line.split()
        if len(parts) < 4:
            continue
        fname = parts[-1][2:]  # strip leading "b/"
        if fname in exclude or fname in seen:
            continue
        seen.add(fname)
        files.append(fname)
    return files


def _parse_summary_files(summary_text: str, step_id: str) -> set[str]:
    """Extract backtick-quoted file paths from a step_summary.md header.

    The SKILL.md format specifies ``- {step_id}: Adapted — <files>`` where
    ``<files>`` is a comma-separated list of backtick-quoted paths.  This
    header line was previously ignored by ``generate_final_post``'s parser.
    Returns the set of paths mentioned on the header line; empty set if the
    header is missing or contains no backticks.
    """
    pattern = re.compile(rf"^- {re.escape(step_id)}:\s*Adapted\s*—\s*(.*)$")
    for line in summary_text.splitlines():
        m = pattern.match(line.strip())
        if m:
            return set(re.findall(r"`([^`]+)`", m.group(1)))
    return set()


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


def _resolve_test_timeouts() -> dict[str, int] | None:
    """Per-test timeout overrides from test_policy.json's "timeouts" map.

    Tests that hang (env-bound failures, broken collection) would otherwise
    burn the full MAIN2MAIN_TEST_TIMEOUT (30 min) per e2e round.
    """
    policy_path = Path(__file__).parent / "test_policy.json"
    if not policy_path.exists():
        return None
    try:
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, KeyError):
        return None
    timeouts = policy.get("timeouts", {})
    if not isinstance(timeouts, dict):
        return None
    out: dict[str, int] = {}
    for k, v in timeouts.items():
        if isinstance(k, str) and isinstance(v, int) and v > 0:
            out[k] = v
    return out or None


def _has_source_changes(changed_files: list[str]) -> bool:
    """True if any changed file is real adaptation code.

    Steps that only bump .github/vllm-main-verified.commit (or touch
    docs/configs) don't need an e2e run — previously they ran the full
    suite (~40 min) for nothing.
    """
    for f in changed_files:
        if not f:
            continue
        if f.startswith(".github/"):
            continue
        if f.endswith((".md", ".json", ".txt", ".yaml", ".yml", ".toml", ".cfg")):
            continue
        return True
    return False


_UPSTREAM_DIFF_KEEP = (
    "vllm/model_executor/",
    "vllm/lora/",
    "vllm/v1/",
    "vllm/distributed/",
    "vllm/_custom_ops.py",
    "vllm/envs.py",
    "vllm/config/",
    "vllm/platforms/",
    "vllm/attention/",
)


def _build_upstream_fix_diff(vllm_path: str, start_commit: str, end_commit: str,
                             max_chars: int = 25000) -> str:
    """Upstream diff (start..end) restricted to runtime code paths.

    The full upstream patch exists on disk ({patch_path}) but the adapter
    routinely skips it when it is large.  A targeted diff of the paths that
    can actually affect e2e behavior makes the change surface visible —
    run 32101793062: the adapter dismissed 47ececb58e's shared-expert
    stream-sync refactor (moe_runner/shared_experts) as "no-op because
    forward is overridden", while the ms-ON path failed 9 rounds on
    stale-buffer races.  The wrapped modules' async/stream/event logic still
    executes on the Ascend wrapper's behalf.
    """
    pathspec = []
    for p in _UPSTREAM_DIFF_KEEP:
        pathspec.append(p if p.endswith("/") else p)
    r = subprocess.run(
        ["git", "diff", f"{start_commit}..{end_commit}", "--", *pathspec],
        cwd=vllm_path, capture_output=True, text=True,
    )
    diff = r.stdout
    if len(diff) <= max_chars:
        return diff
    return diff[:max_chars] + "\n... [truncated]"


def _revert_e2e_test_edits(ascend_path: str) -> list[str]:
    """Revert any adapter edits under tests/e2e/ — E2E test cases are frozen.

    Returns the reverted paths.  Tracked-file changes are checked out;
    newly created files/dirs are removed.  Run 32101793062: the adapter
    rewrote test_basic.py's dspark golden values to the measured failure
    (relaxing the assertion); without this guard the "fix" would have
    shipped in the adaptation PR.
    """
    e2e_dir = Path(ascend_path) / "tests" / "e2e"
    if not e2e_dir.exists():
        return []
    r = subprocess.run(
        ["git", "status", "--short", "--", "tests/e2e/"],
        cwd=ascend_path, capture_output=True, text=True,
    )
    reverted: list[str] = []
    for line in r.stdout.splitlines():
        st, path = line[:2], line[3:].strip()
        if not path or path.startswith('"'):
            continue
        full = Path(ascend_path) / path
        if st == "??":
            if full.is_dir():
                shutil.rmtree(full)
            elif full.is_file() or full.is_symlink():
                full.unlink()
        else:
            subprocess.run(["git", "checkout", "--", path],
                           cwd=ascend_path, capture_output=True, text=True)
        reverted.append(path)
    return reverted


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

    # Tracked from detect step for PR title / push
    base_commit: str = ""

    # Changed files from current adaptation step (for precise test selection)
    changed_files: list[str] = []

    # Whether the LAST step's e2e ran and passed.  The last step's e2e runs
    # on the accumulated state (all prior steps' commits + this step's patch),
    # so it subsumes earlier steps.  When the last step skipped its e2e (or
    # failed), the accumulated state is unvalidated and the final quality gate
    # must run the regression e2e — the agent's no-op judgment can be wrong.
    last_step_e2e_passed: bool = False

    # Set by _ai_analysis: True if the adapter's analysis found the step
    # needs no vllm-ascend code change (modified files excluding the
    # tracking file).  process_steps uses this to skip the per-step e2e
    # round; final_quality_gate's regression e2e still runs.
    last_step_is_noop: bool = False

    # Persistent opencode session ID for full conversational context
    session_id: str = ""

    # Persistent QA session — reused across retries so the reviewer doesn't
    # re-read the entire codebase from scratch on every attempt.
    qa_session_id: str = ""

    # Last vllm commit that actually passed e2e tests (not just was adapted)
    last_verified_commit: str = ""

    # Local path to vllm-report checkout (cloned in initialize). Empty if
    # clone failed - adapter degrades to grep-based code exploration.
    vllm_report_path: str = ""

    # Second vllm checkout at the pinned release tag (e.g. v0.26.0, read
    # from vllm-ascend's .github/vllm-release-tag.commit).  Used by the UT
    # gate to test the release branch alongside main.  Empty if worktree
    # creation failed — UT gate tests main only.
    vllm_release_path: str = ""

    # External E2E: the ready-all test groups computed from the accumulated
    # tree (reused across the gate regression — the test set rarely changes).
    e2e_groups: list = []


class Main2MainFlow:

    def __init__(self, **kwargs):
        self.state = Main2MainState(**kwargs)

    def _run_adapter_qa(
        self, ascend_path: str, vllm_path: str, step_id: str,
        step_dir: str, release_tag: str,
        upstream_patch_path: str = "",
        qa_session_id: str = "",
    ) -> tuple[list[str], str]:
        """adapter-qa: independent review of the current diff."""
        diff = subprocess.run(
            ["git", "diff", "HEAD"], cwd=ascend_path,
            capture_output=True, text=True,
        ).stdout.strip()
        if not diff:
            return [], qa_session_id

        lessons_path = Path(__file__).parent / "agents" / "adapter-qa" / "reference" / "review-lessons.md"
        if not lessons_path.exists():
            return [], qa_session_id
        qa_template_path = Path(__file__).parent / "agents" / "adapter-qa" / "SKILL.md"
        qa_template = ""
        if qa_template_path.exists():
            qa_template = qa_template_path.read_text(encoding="utf-8")

        lessons = lessons_path.read_text(encoding="utf-8")
        # Inject only the checklist skeleton (section/subsection headings) —
        # the full review-lessons.md (with the classic examples) stays at
        # {lessons_path} for on-demand reading.  Inlining all of it (~6KB)
        # made every QA tool-call generation re-process the examples, and QA
        # sessions ran 128 tool calls against a 53K-char prompt.
        checklist = "\n".join(
            line for line in lessons.splitlines()
            if line.startswith("## ") or line.startswith("### ")
        ) + f"\n\nFull details with examples: {lessons_path}"

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

        review_json = Path(review_path)
        if review_json.exists():
            try:
                review = json.loads(review_json.read_text(encoding="utf-8"))
                verdict = review.get("verdict", "")
                issues = review.get("issues", [])
                if verdict == "pass":
                    ts_print(f"[adapter-qa] {step_id}: pass")
                    return [], new_session_id
                ts_print(f"[adapter-qa] {step_id}: fail — {len(issues)} issue(s)")
                return ([f"{i.get('file', '?')}:{i.get('line', '?')}: {i.get('issue', '?')}" for i in issues],
                        new_session_id)
            except (json.JSONDecodeError, KeyError):
                ts_print(f"[adapter-qa] {step_id}: fail (review.json unparseable)")
                return ["critic: review.json could not be parsed"], new_session_id

        ts_print(f"[adapter-qa] {step_id}: fail (no review.json found)")
        return ["critic: no review.json found — opencode did not produce expected output"], new_session_id

    def _e2e_cfg(self) -> E2EDispatchConfig:
        """Build the external-E2E config, resolving the vllm ref when the
        run has no explicit TARGET_COMMIT (scheduled runs test vllm main —
        use the checkout's HEAD, the same commit the workflow checked out)."""
        cfg = E2EDispatchConfig.from_env(self.state.target_commit)
        if not cfg.vllm:
            cfg.vllm = run_git(self.state.vllm_path,
                               "rev-parse", "HEAD").strip()
        return cfg

    def run(self, inputs: dict | None = None):
        if inputs:
            for k, v in inputs.items():
                setattr(self.state, k, v)
        self.initialize()
        self._warmup_mega_moe()
        # External E2E: pre-start the three runners' environment prep in
        # parallel with the main flow, so the first per-step E2E round does
        # not wait for csrc builds / dependency installs.  The workflow
        # dispatches the prep as its step 0 (earliest possible) and records
        # the run id; reuse it instead of double-dispatching.
        if os.getenv("MAIN2MAIN_E2E_REPO"):
            try:
                e2e_cfg = self._e2e_cfg()
            except Exception as exc:
                ts_print(f"[e2e] external E2E config invalid ({exc})")
                e2e_cfg = None
            if e2e_cfg is not None and e2e_cfg.vllm:
                try:
                    env_prep = os.getenv("MAIN2MAIN_E2E_PREP_RUN_ID", "")
                    if env_prep.isdigit():
                        ts_print(f"[e2e] reusing workflow-dispatched prep "
                                 f"run {env_prep}")
                    else:
                        dispatch_prep(e2e_cfg)
                except Exception as exc:
                    ts_print(f"[e2e] prep dispatch FAILED ({exc}) — exec "
                             f"rounds will inline-setup until the env exists")
        signal = self.analyze_commit_and_plan_step()
        if signal == HasNoCommit:
            self.has_no_commit()
            return
        self.process_steps()
        # The quality gate runs only when at least one step succeeded (the
        # main-branch rule).  Per-step E2E failures were already handled
        # inside process_steps (fix rounds -> revert + UpgradeFailed), so
        # reaching this point with current_step > 0 means every successful
        # step was verified on the external A2/A3 runners.
        gate_passed = True
        if self.state.current_step > 0:
            gate_passed = self._final_quality_gate()
            if not gate_passed:
                self.state.final_status = UpgradeFailed
        self.generate_final_post()
        # Persist adaptation lessons (E2E fix rounds) back to vllm-report
        # before push — the clone is recreated every run, so unsaved
        # lessons would be lost.
        persist_lessons(self.state.vllm_report_path)
        self._cleanup_release_worktree()
        if self.state.current_step == 0:
            # No step ever passed e2e: there is no successful adaptation to
            # submit, so creating a PR would only ship a "failed" description
            # and the last-attempted (broken) diff (PR #14376).  Skip the
            # push entirely.
            ts_print("[push] no steps completed, skipping PR creation")
            return
        if not gate_passed:
            # Format/mypy/UT could not be satisfied after the gate's fix
            # rounds — pushing would ship known-broken code (run 174's E501).
            # The manual review issue is created by the workflow's
            # final-status step; do not create a PR.
            ts_print("[push] final quality gate failed after fix rounds, "
                     "skipping PR creation")
            return
        self.push_to_github()

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

        vllm_branch = run_git(self.state.vllm_path, "branch", "--show-current").strip()
        self.state.original_vllm_ref = vllm_branch or run_git(self.state.vllm_path, "rev-parse", "HEAD").strip()
        # PR-level squash baseline.  schedule_main2main.yaml pins
        # refs/remotes/upstream/main to the upstream SHA this run's branch was
        # rebased onto — exactly the PR base.  Using the checkout HEAD instead
        # leaves previous runs' sync commits (accumulated one per run on the
        # reused main2main_baseline) BELOW the baseline, so the PR grows one
        # commit per run (PR #13767 had 4).  Fall back to HEAD when
        # upstream/main is unavailable (local runs) or not an ancestor.
        ascend_head = run_git(self.state.vllm_ascend_path, "rev-parse", "HEAD").strip()
        self.state.original_ascend_ref = resolve_squash_baseline(
            self.state.vllm_ascend_path, ascend_head)
        ts_print(f"[init] squash baseline: {self.state.original_ascend_ref[:12]} "
                 f"(HEAD={ascend_head[:12]})")

        # Fix pre-commit hook permissions: git doesn't track +x, so gitleaks.sh
        # is not executable after checkout, causing spurious format.sh failures.
        gitleaks_script = Path(self.state.vllm_ascend_path) / ".github/workflows/scripts/gitleaks.sh"
        if gitleaks_script.exists():
            gitleaks_script.chmod(0o755)

        # Clone vllm-report knowledge base (shallow) for adapter context.
        # Non-fatal: if clone fails, adapter degrades to grep-based exploration.
        try:
            report_url = "https://github.com/vllm-ascend/vllm-report.git"
            report_target = WORKSPACE_DIR / "repos" / "vllm-report"
            if report_target.exists():
                shutil.rmtree(report_target)
            report_target.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                ["git", "clone", "--depth", "1", report_url, str(report_target)],
                check=True, capture_output=True, text=True,
            )
            self.state.vllm_report_path = str(report_target)
            ts_print(f"\n[init] vllm-report cloned to {report_target}")

            # Install mcp dependency for vllm-report's MCP server.
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q",
                 "mcp>=2.0.0", "anyio>=4.0.0"],
                capture_output=True, text=True,
            )

            # Write opencode.jsonc to vllm-ascend repo root (flow's cwd),
            # registering vllm-report MCP server.  opencode auto-reads this
            # config file from cwd, so the adapter can call vllm-report's
            # MCP tools (get_adaptation_guide, get_cross_project_mapping,
            # search_analysis, etc.) on-demand during adaptation.
            mcp_config = {
                "$schema": "https://opencode.ai/config.json",
                "mcp": {
                    "vllm-report": {
                        "type": "local",
                        "command": [
                            sys.executable,
                            "-m",
                            "src.mcp_server_app",
                            "--data-dir",
                            str(report_target / "data"),
                            "--ascend-repo-path",
                            str(self.state.vllm_ascend_path),
                        ],
                        # cwd must be the vllm-report repo root so Python
                        # finds the `src` package (python -m src.mcp_server_app
                        # requires the package to be importable from cwd).
                        # Per vllm-report/docs/mcp-usage-guide.md.
                        "cwd": str(report_target),
                        "enabled": True,
                    }
                }
            }
            # Write opencode config to BOTH the global config dir and the
            # vllm-ascend repo root:
            #  1. ~/.config/opencode/opencode.jsonc — opencode's global config
            #     (user-verified locally: .jsonc works in this location).
            #  2. <repo root>/opencode.json + OPENCODE_CONFIG env var —
            #     belt-and-suspenders; OPENCODE_CONFIG is opencode's explicit
            #     custom-config path, guaranteeing the file is read.
            # The MCP command embeds absolute /tmp paths, so the repo-root
            # file MUST NOT be committed into the adaptation PR.  The first
            # step commit runs `git add -A`, which would stage it — append to
            # .git/info/exclude so git never tracks it.
            # Global config: merge (don't clobber) any existing user config.
            global_cfg_dir = Path.home() / ".config" / "opencode"
            global_cfg_path = global_cfg_dir / "opencode.jsonc"
            global_cfg_dir.mkdir(parents=True, exist_ok=True)
            if global_cfg_path.exists():
                try:
                    global_cfg = json.loads(global_cfg_path.read_text(encoding="utf-8"))
                    if not isinstance(global_cfg, dict):
                        global_cfg = {}
                except (json.JSONDecodeError, OSError):
                    global_cfg = {}
            else:
                global_cfg = {}
            global_cfg.setdefault("mcp", {})
            global_cfg["mcp"]["vllm-report"] = mcp_config["mcp"]["vllm-report"]
            global_cfg_path.write_text(
                json.dumps(global_cfg, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            # Project root config (belt-and-suspenders + OPENCODE_CONFIG).
            opencode_config_path = Path(self.state.vllm_ascend_path) / "opencode.json"
            opencode_config_path.write_text(
                json.dumps(mcp_config, indent=2) + "\n", encoding="utf-8"
            )
            # Set OPENCODE_CONFIG so opencode definitely reads this file
            # (opencode's custom-config path, highest precedence).
            os.environ["OPENCODE_CONFIG"] = str(opencode_config_path)
            # .git/info/exclude (not .gitignore) - local-only, doesn't pollute
            # the repo or the PR diff.  Also excludes generated tool artifacts
            # (torch_compile_debug/ etc.) so they can never enter a step
            # commit, the gate checks, or the PR diff.
            exclude_file = Path(self.state.vllm_ascend_path) / ".git" / "info" / "exclude"
            exclude_patterns = ["opencode.json", *GENERATED_ARTIFACT_DIRS]
            try:
                exclude_content = exclude_file.read_text(encoding="utf-8")
                missing = [p for p in exclude_patterns if p not in exclude_content]
                if missing:
                    exclude_file.write_text(
                        exclude_content.rstrip() + "\n" + "\n".join(missing) + "\n",
                        encoding="utf-8",
                    )
            except OSError:
                # .git/info/exclude not writable - append to .gitignore instead
                # so opencode.json still doesn't get committed into the PR.
                gitignore_path = Path(self.state.vllm_ascend_path) / ".gitignore"
                try:
                    gi_content = gitignore_path.read_text(encoding="utf-8")
                    missing = [p for p in exclude_patterns if p not in gi_content]
                    if missing:
                        gitignore_path.write_text(
                            gi_content.rstrip() + "\n" + "\n".join(missing) + "\n",
                            encoding="utf-8",
                        )
                        ts_print("[init] added patterns to .gitignore (exclude not writable)")
                except OSError:
                    ts_print("[init] WARNING could not ignore opencode.json - "
                             "it may be committed into the PR!")
            ts_print(f"\n[init] vllm-report MCP server registered in "
                     f"{global_cfg_path} (global) + {opencode_config_path} "
                     f"(OPENCODE_CONFIG={os.environ['OPENCODE_CONFIG']})")
            # Verify the MCP server actually starts with the same command
            # opencode will use (python -m src.mcp_server_app from the
            # vllm-report root).  A stdio server stays alive waiting on stdin.
            # IMPORTANT: pipe stdin (stdin=PIPE) — if the child inherits the
            # parent's stdin (closed /dev/null in CI), the stdio server reads
            # EOF and exits 0 immediately, falsely reporting "FAILED".  With
            # stdin held open, an alive process after a few seconds = OK.
            # NOTE: do NOT use communicate() here — it CLOSES the stdin
            # pipe, the stdio server reads EOF and exits 0 (the false
            # "FAILED" we saw in the first PR #13657 run).  wait() keeps
            # stdin open; a TimeoutExpired means the server is alive.
            try:
                vproc = subprocess.Popen(
                    [sys.executable, "-m", "src.mcp_server_app",
                     "--data-dir", str(report_target / "data"),
                     "--ascend-repo-path", str(self.state.vllm_ascend_path)],
                    cwd=str(report_target), stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                    text=True,
                )
                try:
                    rc = vproc.wait(timeout=5)
                    # Process exited within 5s = startup FAILED.
                    vstderr = ""
                    if vproc.stderr:
                        vstderr = vproc.stderr.read().strip()
                    ts_print(f"[init] WARNING vllm-report MCP server FAILED "
                             f"to start (exit={rc}): {vstderr[:300]}")
                except subprocess.TimeoutExpired:
                    # Still alive after 5s waiting on stdin = startup OK.
                    ts_print("[init] vllm-report MCP server startup verified "
                             "OK (alive 5s waiting on stdio)")
                    vproc.kill()
                    vproc.wait()
                    if vproc.stderr:
                        vproc.stderr.close()
            except OSError as e:
                ts_print(f"[init] WARNING vllm-report MCP server FAILED to "
                         f"start: {e}")
        except (subprocess.CalledProcessError, OSError) as e:
            self.state.vllm_report_path = ""
            ts_print(f"\n[init] vllm-report clone failed (adapter will use grep): {e}")

        # Prepare a second vllm checkout at the PINNED RELEASE tag (read from
        # vllm-ascend's .github/vllm-release-tag.commit, e.g. "v0.26.0"),
        # so the UT gate can test BOTH the target main and the release
        # branch.  vllm-ascend carries vllm_version_is("<release_tag>")
        # guards — a fix that passes on main can break the release branch.
        # Uses a git worktree (shares objects with the main clone, only
        # fetches the tag once).  Non-fatal: failure degrades to main-only.
        self.state.vllm_release_path = ""
        try:
            ascend_repo = Path(self.state.vllm_ascend_path)
            tag_file = ascend_repo / ".github" / "vllm-release-tag.commit"
            if tag_file.exists():
                release_tag = tag_file.read_text(encoding="utf-8").strip()
                vllm_repo = Path(self.state.vllm_path)
                release_worktree = WORKSPACE_DIR / "repos" / "vllm-release"
                if release_worktree.exists():
                    shutil.rmtree(release_worktree)
                # Fetch the tag if not present (depth 1 keeps it fast).
                subprocess.run(
                    ["git", "fetch", "--depth", "1", "origin",
                     f"refs/tags/{release_tag}:refs/tags/{release_tag}"],
                    cwd=str(vllm_repo), capture_output=True, text=True,
                    timeout=300,
                )
                subprocess.run(
                    ["git", "worktree", "add", "-f", "--detach",
                     str(release_worktree), release_tag],
                    cwd=str(vllm_repo), capture_output=True, text=True,
                    timeout=120, check=True,
                )
                self.state.vllm_release_path = str(release_worktree)
                ts_print(f"\n[init] vllm release worktree at {release_worktree} "
                         f"({release_tag}) for dual-version UT")
        except (subprocess.CalledProcessError, OSError) as e:
            self.state.vllm_release_path = ""
            ts_print(f"[init] WARNING vllm release worktree failed ({e}) — "
                     "UT gate will test main only")

    def _warmup_mega_moe(self) -> None:
        """Pre-compile CANN 9.1.0's mega_moe op so the first e2e doesn't JIT it.

        CANN 9.1.0's cann_ops_transformer mega_moe op (used by MoE + EP + EPLB
        tests like qwen3_30b_a3b) JIT-compiles npu_mega_moe.so at first import
        (~4 min of c++).  During compilation the rank's shm_broadcast blocks
        for >60s and the HCCL watchdog kills the engine (run 31515866004,
        31504773494).  Pre-importing the module here compiles it once, before
        any test starts.
        """
        if os.getenv("MAIN2MAIN_SKIP_NPU_WARMUP", "false").lower() == "true":
            ts_print("[init] MAIN2MAIN_SKIP_NPU_WARMUP=true, "
                     "skipping mega_moe warmup")
            return
        if shutil.which("npu-smi") is None:
            ts_print("[init] npu-smi not found (CPU runner) — skipping "
                     "mega_moe warmup")
            return
        try:
            ts_print("[init] warming up CANN mega_moe op (JIT compile once)...")
            subprocess.run(
                [sys.executable, "-c",
                 "import cann_ops_transformer.ops.mega_moe; print('mega_moe warmed')"],
                capture_output=True, text=True, timeout=900,
            )
            ts_print("[init] mega_moe warmup done")
        except (subprocess.TimeoutExpired, OSError) as e:
            ts_print(f"[init] WARNING mega_moe warmup failed ({e}) — "
                     "first MoE/EP test may JIT-compile at runtime")

    def _cleanup_release_worktree(self) -> None:
        """Remove the vllm release worktree created in initialize."""
        if not self.state.vllm_release_path:
            return
        try:
            subprocess.run(
                ["git", "worktree", "remove", "-f",
                 self.state.vllm_release_path],
                cwd=self.state.vllm_path, capture_output=True, text=True,
                timeout=60,
            )
            ts_print("[init] removed vllm release worktree")
        except (subprocess.CalledProcessError, OSError) as e:
            ts_print(f"[init] WARNING failed to remove release worktree: {e}")
        self.state.vllm_release_path = ""

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
        plan = run_plan(vllm_path, result["base_commit"], result["target_commit"],
                        vllm_report_path=Path(self.state.vllm_report_path) if self.state.vllm_report_path else None,
                        ascend_path=Path(self.state.vllm_ascend_path))
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

    def has_no_commit(self):
        ts_print(f"[done] 仓库已同步，无需适配，流程结束。")

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
                self._revert_working_tree(f"step {step_id} ai_analysis exhausted")
                self.state.final_status = UpgradeFailed
                return

            # Adapter analyzed the step and confirmed no vllm-ascend code
            # change needed → skip the per-step e2e round (only on the first
            # attempt; if a later step's regression e2e in the final gate
            # surfaces a baseline bug, that gate's fix loop handles it).
            # The final quality gate always runs the regression e2e at the end.
            if self.state.last_step_is_noop and self.state.retry_count == 0:
                ts_print(f"[process_steps] {step_id}: adapter marked step as "
                         f"no-op (no vllm-ascend code change), skipping e2e")
                # Commit the verified.commit bump + any adapter artifacts
                # (e.g. step_summary.md is untracked, no commit needed for it).
                run_git(ascend_path, "add", "-A")
                subprocess.run(["git", "commit", "-s", "-m",
                                 f"main2main: step {step_id} ({step['end_commit'][:8]})"],
                                cwd=ascend_path, capture_output=True)
                self.state.current_step += 1
                self.state.retry_count = 0
                self.state.last_verified_commit = self.state.cur_vllm_commit
                self.state.last_step_e2e_passed = False  # final gate must run regression
                continue

            test_pass = self._run_e2e_test()
            if test_pass:
                # The step needed >=1 E2E fix round (retry_count >= 1): the
                # first adaptation wasn't right — record it as a lesson so
                # future runs fix it in one pass (persisted + pushed at the
                # end of the run by persist_lessons).
                if self.state.retry_count >= 1:
                    submit_step_lesson(self.state.vllm_report_path, step_id)
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
                    # Revert the broken adaptation (passed pre_ci/critic but
                    # failed e2e) so it doesn't leak into generate_final_post's
                    # squash via `git add -A`. Same revert as Path A above.
                    ts_print(f"[process_steps] {step_id}: e2e exhausted retries, "
                             f"reverting to last committed state")
                    self._revert_working_tree(f"step {step_id} e2e exhausted")
                    self.state.final_status = UpgradeFailed
                    return
                continue
        self.state.final_status = UpgradeCompleted

    def _capture_step_patch(self, ascend_path: str, step_dir: Path,
                            step_id: str) -> None:
        """Capture the working-tree diff as step_target.patch and set state.

        Used by the no-adaptation branches (SKIP_AI_ANALYSIS / empty upstream
        patch) where cur_patch_path must point at an existing file for
        downstream consumers (signal-branch push, adapter analysis).
        """
        subprocess.run(["git", "add", "-N", "."], cwd=ascend_path,
                       capture_output=True)
        adaptation_patch = run_git(ascend_path, "diff", "HEAD")
        (step_dir / EACH_STEP_TARGET_PATCH_FILE).write_text(
            adaptation_patch, encoding="utf-8")
        ascend_head = run_git(ascend_path, "rev-parse", "HEAD").strip()
        step = self.state.steps[self.state.current_step]
        self.state.cur_vllm_commit = step["end_commit"]
        self.state.cur_ascend_commit = ascend_head
        self.state.cur_patch_path = str(step_dir / EACH_STEP_TARGET_PATCH_FILE)
        # No adaptation changes - clear stale changed_files from a prior step
        # so e2e test selection doesn't filter by the wrong files.
        self.state.changed_files = []

    def _revert_working_tree(self, reason: str) -> None:
        """Discard uncommitted working-tree changes (broken adaptations or
        failed gate fixes) so they don't leak into generate_final_post's
        squash via `git add -A`."""
        ts_print(f"[flow] reverting working tree: {reason}")
        # reset -q clears the index (staged content + `git add -N` intent-to-add
        # entries) that checkout/clean don't remove.
        run_git(self.state.vllm_ascend_path, "reset", "-q")
        run_git(self.state.vllm_ascend_path, "checkout", "--", ".")
        subprocess.run(["git", "clean", "-fd"],
                       cwd=self.state.vllm_ascend_path, capture_output=True)

    def _final_quality_gate(self) -> bool:
        """Run format + mypy on final diff; fix + re-run e2e on failure.

        Returns True if quality gate passes (possibly after fixes), False if
        3 fix rounds exhausted without passing.
        """
        ascend_path = self.state.vllm_ascend_path
        vllm_path = self.state.vllm_path
        # Use a dedicated dir under workspace for quality_gate artifacts.
        gate_dir = WORKSPACE_DIR / "quality_gate"
        gate_dir.mkdir(parents=True, exist_ok=True)

        error_logs: list[str] = []
        # 4 gate runs: attempts 1-3 each feed one adapter fix round; the
        # 4th run VERIFIES the last fix (a fix that never gets re-checked
        # is indistinguishable from failure — run 31691299310's attempt-3
        # fix was correct and PR CI passed, but the gate had already
        # exhausted).
        for attempt in range(1, 5):
            passed, new_error_logs = run_final_quality_gate(
                ascend_path=ascend_path,
                vllm_path=vllm_path,
                vllm_release_path=self.state.vllm_release_path,
                release_tag=self.state.release_tag,
                log_dir=gate_dir,
            )
            if passed:
                if attempt > 1 or not self.state.last_step_e2e_passed:
                    # attempt > 1: we fixed something — confirm format/mypy
                    # edits didn't break functionality.  last_step_e2e_passed
                    # False: the LAST step skipped its per-step e2e (no-op
                    # judgment may be wrong) or failed — the accumulated state
                    # is unvalidated, so the regression e2e is the last
                    # guarantee before push.
                    ts_print(f"[final_quality_gate] fix attempt {attempt}: passed "
                             f"(last step e2e={'passed' if self.state.last_step_e2e_passed else 'skipped/failed'}), "
                             f"running regression e2e")
                    if not self._run_e2e_test_for_final_gate():
                        # Revert the regression-inducing fix so it doesn't get
                        # pushed (KEEP_BRANCH mode does `git add -A` + amend),
                        # then continue the fix loop with remaining rounds.
                        # The gate will re-run format+mypy on the reverted tree.
                        ts_print(f"[final_quality_gate] e2e regression - "
                                 f"reverting, continuing to next round")
                        self._revert_working_tree("gate e2e regression")
                        error_logs = [str(Path(gate_dir) / "quality_gate.json")]
                        continue
                if attempt > 1:
                    # A fix round succeeded — record the failure knowledge
                    # (version guards, test isolation, etc.) so future runs
                    # fix the same gate failure in one pass.
                    submit_gate_lesson(self.state.vllm_report_path, error_logs)
                ts_print(f"\n[final_quality_gate] PASSED (attempt {attempt})")
                # Regenerate the accumulated patch from the CURRENT working
                # tree so it includes format/mypy fixes made by the gate.
                # Use `git diff <original_ascend_ref>` (baseline -> working
                # tree): `git diff HEAD` would only contain uncommitted fixes
                # (steps are already committed).  Write to a dedicated file;
                # generate_final_post prefers it over the pre-gate step patch.
                subprocess.run(["git", "add", "-N", "."], cwd=ascend_path,
                               capture_output=True)
                gate_patch = run_git(
                    ascend_path, "diff", self.state.original_ascend_ref)
                (WORKSPACE_DIR / "gate_final_patch").write_text(
                    gate_patch, encoding="utf-8")
                ts_print(f"[final_quality_gate] regenerated gate_final_patch "
                         f"({len(gate_patch.splitlines())} lines) with gate fixes")
                return True

            error_logs = new_error_logs
            if attempt == 4:
                # Verification run for the last fix — no further fix round.
                break
            ts_print(f"\n[final_quality_gate] fix attempt {attempt}/3: FAILED "
                     f"-> adapter-fix")

            role = "adapter-fix"
            ts_print(f"[final_quality_gate] opencode attempt {attempt}, role={role}")
            adapt_result = run_opencode_adapter({
                "step_id": "final-quality-gate",
                "previous_step_id": "",
                "previous_step_summary_path": "",
                "is_last_step": "true",
                "step_dir": str(gate_dir),
                "patch_path": "",
                "changed_files_path": "",
                "ascend_path": ascend_path,
                "release_tag": self.state.release_tag,
                "vllm_path": vllm_path,
                "role": role,
                "error_logs": json.dumps(error_logs, ensure_ascii=False),
                "code_structure_guide_file": EACH_STEP_CODE_STRUCTURE_GUIDE_FILE,
                "mode": role,
                # The gate's fix rounds fix UT/test failures (e.g. PIN_MEMORY,
                # maybe_calc_kv_scales, deepseek_v4_thinking) — the adapter
                # must query vllm-report's lessons (get_adaptation_lessons) to
                # fix them in one pass instead of blind retries.
                "vllm_report_context": (
                    "vllm-report MCP server is registered in opencode.jsonc. "
                    "Call its tools dynamically (see \"vllm-report MCP Tools\" "
                    "section below).  Call tool_get_adaptation_lessons to find "
                    "prior lessons matching these failures before fixing."
                ),
            }, session_id=self.state.session_id)
            if adapt_result.session_id:
                self.state.session_id = adapt_result.session_id

        ts_print("\n[final_quality_gate] exhausted 3 fix rounds, still failing")
        # KEEP the adapter's last fixes in the working tree.  Reverting used
        # to discard ALL of them — but the fix loop can resolve most issues
        # (e.g. the PIN_MEMORY / maybe_calc_kv_scales test adaptations that
        # a fresh run keeps missing: attempt 3 passed main/batch 2068/2068)
        # and leave only a few env-bound failures.  Throwing the good fixes
        # away made the pushed PR carry the UNFIXED tests and its CI went red
        # on the same errors the adapter had already fixed.  With the tree
        # kept, remaining failures stay visible in the PR CI.
        return False

    def _run_e2e_test_for_final_gate(self) -> bool:
        """Re-run e2e after final-quality-gate fixes to confirm no regression.

        Key differences from per-step _run_e2e_test:
        - SKIP_PIP_INSTALL=true: vllm package didn't change (only vllm-ascend
          code was edited by format/mypy fixes), skip the 2-3min reinstall.
        - Regenerate patch from current working tree (format/mypy fixes are
          in the working tree, not in the old step_target.patch).
        - Force MAIN2MAIN_KEEP_BRANCH=true so setup_env doesn't reset
          vllm-ascend (which would discard the format/mypy fixes).
        """
        if not self.state.steps:
            return True
        if os.getenv("SKIP_E2E_TEST", "false").lower() == "true":
            ts_print("[final_quality_gate] SKIP_E2E_TEST=true, skipping regression e2e")
            return True

        ascend_path = self.state.vllm_ascend_path
        vllm_path = self.state.vllm_path
        step = self.state.steps[-1]
        step_id = step["id"]

        # Regenerate patch from current working tree (post format/mypy fix).
        subprocess.run(["git", "add", "-N", "."], cwd=ascend_path, capture_output=True)
        new_patch = run_git(ascend_path, "diff", "HEAD")
        gate_dir = WORKSPACE_DIR / "quality_gate"
        patch_path = gate_dir / "final_gate.patch"
        patch_path.write_text(new_patch, encoding="utf-8")
        ts_print(f"[final_quality_gate] regenerated patch ({len(new_patch)} bytes) "
                 f"for regression e2e")

        # Use the ACCUMULATED changed files (baseline -> final tree) for test
        # selection.  The last step's changed_files only covers that step, but
        # gate format/mypy fixes can touch ANY file in the repo (mypy runs the
        # whole tree), so the regression e2e must cover all accumulated changes.
        accumulated_files = run_git(
            ascend_path, "diff", "--name-only", self.state.original_ascend_ref
        ).strip().splitlines()
        accumulated_files = [f for f in accumulated_files if f]

        # External E2E: the working tree (format/mypy fixes included) is
        # pushed to the signal branch and the exec workflow re-runs the
        # ready-all suite on the already-prepared environments — no
        # reinstall.  The gate's fix loop (revert -> adapter-fix -> retry)
        # stays compatible: each attempt re-pushes the current tree.
        return self._run_external_gate_regression(step_id, accumulated_files)

    def _run_external_gate_regression(self, step_id,
                                      accumulated_files: list[str]) -> bool:
        """Gate regression on the external A2/A3 runners (round 0).

        The working tree — format/mypy fixes included, whether committed or
        not — is pushed to the signal branch and the exec workflow re-runs
        the ready-all suite on the already-prepared runner environments.
        Reuses the computed test groups when available (gate fixes rarely
        change the test set); recomputes otherwise.
        """
        ascend_path = self.state.vllm_ascend_path
        cfg = self._e2e_cfg()
        if not os.getenv("MAIN2MAIN_E2E_REPO") or not cfg.vllm:
            ts_print("[final_quality_gate] external E2E requested but "
                     f"MAIN2MAIN_E2E_REPO={'set' if os.getenv('MAIN2MAIN_E2E_REPO') else 'unset'}, "
                     "vllm commit (TARGET_COMMIT) missing — regression "
                     "treated as failed")
            return False
        if not self.state.e2e_groups:
            base_sha = self.state.original_ascend_ref or \
                resolve_squash_baseline(ascend_path)
            try:
                self.state.e2e_groups = compute_test_groups(
                    Path(ascend_path), base_sha, accumulated_files)
            except Exception as exc:
                ts_print(f"[final_quality_gate] compute_test_groups failed: "
                         f"{exc}")
                return False
        try:
            result = run_external_e2e(
                cfg, Path(ascend_path), self.state.e2e_groups,
                WORKSPACE_DIR / STEPS_DIR, round_number=0, step_id=step_id)
        except Exception as exc:
            ts_print(f"[final_quality_gate] regression e2e dispatch FAILED: "
                     f"{exc}")
            return False
        test_passed = result.get("can_commit", False)
        ts_print(f"\n[final_quality_gate] regression e2e: "
                 f"{'PASSED' if test_passed else 'FAILED'}")
        return test_passed

    def _ai_analysis(self) -> bool:
        step = self.state.steps[self.state.current_step]
        step_id = step["id"]
        step_dir = WORKSPACE_DIR / STEPS_DIR / step_id

        # Skip opencode adaptation when upstream_patch is empty (no vllm/ code
        # changes in this step) — but ONLY on the first attempt (retry_count==0).
        # If e2e tests fail on retry, the failure may come from a baseline bug
        # exposed by a newly-added test case.  In that case, fall through to the
        # normal adapter fix-mode loop so the adapter CAN fix the baseline code.
        upstream_patch = step.get("upstream_patch", "")
        if os.getenv("SKIP_AI_ANALYSIS", "false").lower() == "true":
            ts_print(f"[ai_analysis] {step_id}: SKIP_AI_ANALYSIS=true, skipping")
            vllm_path = self.state.vllm_path
            ascend_path = self.state.vllm_ascend_path
            if self.state.retry_count == 0:
                run_git(vllm_path, "checkout", step["end_commit"])
                try:
                    run_update(ascend_path=Path(ascend_path), old_commit=step["start_commit"],
                               new_commit=step["end_commit"])
                except ValueError:
                    ts_print(f"[ai_analysis] {step_id}: commit ref already updated, skipping")
            self._capture_step_patch(ascend_path, step_dir, step_id)
            return True

        # When upstream_patch is empty and this is the first attempt, skip the
        # adapter loop and just advance verified.commit.  If e2e then fails,
        # the retry (retry_count > 0) will fall through to the normal adapter
        # fix-mode loop so the adapter can fix the baseline bug.
        if not upstream_patch.strip() and self.state.retry_count == 0:
            ts_print(f"[ai_analysis] {step_id}: no vllm/ code changes, skipping adaptation")
            vllm_path = self.state.vllm_path
            ascend_path = self.state.vllm_ascend_path
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
            summary_path = step_dir / EACH_STEP_SUMMARY_FILE
            if not summary_path.exists():
                summary_path.write_text(
                    f"- {step_id}: No vllm/ code changes, advanced verified.commit to "
                    f"{step['end_commit'][:8]}\n", encoding="utf-8")
            archive_dir = Path(ascend_path) / ".archive"
            if archive_dir.exists():
                shutil.rmtree(archive_dir)
            reset_r = subprocess.run(
                ["git", "checkout", "--", "."], cwd=vllm_path, capture_output=True, text=True)
            if reset_r.returncode != 0:
                ts_print(f"[ai_analysis] {step_id}: failed to reset vllm: {reset_r.stderr.strip()}")
            self._capture_step_patch(ascend_path, step_dir, step_id)
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

        # vllm-report MCP tools are called dynamically by the adapter during
        # analysis (not pre-loaded as static context here).  The MCP server
        # is registered in opencode.jsonc (see initialize), so the adapter
        # can call tool_get_adaptation_guide / tool_get_cross_project_mapping
        # / tool_get_patch_catalog on-demand.  This avoids the 151-line static
        # context dump that previously inflated prompt size and led the adapter
        # to re-grep everything (it ignored the static context and verified via
        # grep anyway — 0 MCP calls, 31 greps in step-1 of PR #13515's run).
        # Pass only a short pointer so the adapter knows the MCP server is
        # available and which commit to query.
        vllm_report_context = ""
        if self.state.vllm_report_path:
            vllm_report_context = (
                f"vllm-report MCP server is registered in opencode.jsonc. "
                f"Call its tools dynamically (see \"vllm-report MCP Tools\" "
                f"section below). This step's upstream vLLM commit is "
                f"{step['end_commit'][:8]} — call "
                f"tool_get_adaptation_guide(sha=\"{step['end_commit']}\") "
                f"FIRST to get the impact map.")
            ts_print(f"\n[ai_analysis] {step_id}: vllm-report MCP available "
                     f"(dynamic mode, no static context) for {step['end_commit'][:8]}")

        for attempt in range(1, 4):
            role = "adapter-fix" if error_logs else "adapter"
            if role == "adapter-fix":
                # Targeted upstream diff (runtime paths only) — the full
                # upstream patch is skipped when large; a focused diff makes
                # the change surface visible so no-op claims need evidence
                # (multistream misdiagnosis, run 32101793062).
                upstream_diff = _build_upstream_fix_diff(
                    vllm_path, step["start_commit"], step["end_commit"])
                if upstream_diff:
                    fix_ctx = step_dir / "upstream-fix-context.diff"
                    fix_ctx.write_text(
                        "# Upstream diff (step start..end, runtime paths only) — "
                        "check this BEFORE deciding the step is a no-op.\n\n"
                        + upstream_diff,
                        encoding="utf-8")
                    error_logs = list(error_logs) + [str(fix_ctx)]
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
                "vllm_report_context": vllm_report_context,
            }, session_id=self.state.session_id)
            if adapt_result.session_id:
                self.state.session_id = adapt_result.session_id

            # E2E test cases are frozen: any adapter edit under tests/e2e/
            # is reverted immediately and voids the attempt.  The warning is
            # passed back via error_logs so the next attempt knows the rule
            # (run 32101793062: adapter rewrote dspark golden values to the
            # measured failure — a test relaxation, not an adaptation).
            reverted_tests = _revert_e2e_test_edits(ascend_path)
            if reverted_tests:
                warning = (
                    "E2E test cases (tests/e2e/) must NEVER be modified — "
                    "assertions, golden values and parametrizations are frozen. "
                    f"The following edit(s) were reverted: {', '.join(reverted_tests)}. "
                    "Adapt the vllm_ascend/ source code instead; tests/e2e/ files "
                    "cannot be part of the adaptation."
                )
                ts_print(f"[ai_analysis] {step_id}: REVERTED forbidden e2e test "
                         f"edit(s): {reverted_tests}")
                warn_path = step_dir / f"e2e-test-edit-warning-{attempt}.txt"
                warn_path.write_text(warning + "\n", encoding="utf-8")
                error_logs = [str(warn_path)]
                continue

            # pre_ci: mechanical checks (version, format, imports, temp files)
            # vllm_release_path enables symbol-level import checks against
            # the pinned fixed branch (unguarded main-only imports crash it).
            check_result = run_check(
                ascend_path, self.state.release_tag, vllm_path=vllm_path,
                vllm_release_path=self.state.vllm_release_path or None)
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
                review_issues, new_qa_sid = self._run_adapter_qa(
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

            # QA (adapter-qa) gets exactly one fix round: attempt 1 issues are
            # fixed by attempt 2, then e2e is the arbiter regardless of review
            # outcome. Static review cannot settle semantic disputes (e.g.
            # packed-vs-summed KV pools), so iterating it further burns time
            # without converging; e2e verdict is the only signal that matters.
            # pre_ci stays a hard gate across all 3 attempts.
            if pre_ci_passed and (review_passed or attempt >= 2):
                if not review_passed:
                    ts_print(f"[ai_analysis] {step_id}: critic still has issues after "
                             f"1 fix round — proceeding to e2e anyway")
                break

        if not pre_ci_passed:
            ts_print(f"[ai_analysis] {step_id}: FAILED after 3 attempts "
                     f"(pre_ci never passed) — skipping e2e")
            self.state.test_errors = error_logs if error_logs else []
            return False

        self.state.test_errors = []

        # Track whether the adapter made any vllm-ascend code change (excluding
        # the tracking file).  process_steps uses this to skip the per-step
        # e2e for true no-op steps; the final quality gate's regression e2e
        # still runs at the end.
        self.state.last_step_is_noop = bool(adapt_result and adapt_result.is_noop)

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

    def _run_external_step_e2e(self, changed: list[str]) -> bool:
        """Per-step E2E on the external A2/A3 runners."""
        if not os.getenv("MAIN2MAIN_E2E_REPO"):
            ts_print("[run_e2e_test] MAIN2MAIN_E2E_REPO not set — external "
                     "E2E is the only execution path, treating step as failed")
            return False
        step = self.state.steps[self.state.current_step]
        step_id = step["id"]
        ts_print(f"run_e2e_test: {step_id} round={self.state.retry_count} "
                 f"(external)")
        cfg = self._e2e_cfg()
        ascend_path = Path(self.state.vllm_ascend_path)
        base_sha = self.state.original_ascend_ref or \
            resolve_squash_baseline(ascend_path)
        try:
            groups = compute_test_groups(ascend_path, base_sha, changed)
        except Exception as exc:
            ts_print(f"[run_e2e_test] {step_id}: compute_test_groups "
                     f"failed: {exc}")
            return False
        if not groups:
            ts_print(f"[run_e2e_test] {step_id}: no test groups for changed "
                     f"files — nothing to run")
            return True
        result = run_external_e2e(
            cfg, ascend_path, groups, WORKSPACE_DIR / STEPS_DIR,
            self.state.retry_count, step_id=step_id)
        test_passed = result.get("can_commit", False)
        self.state.last_step_e2e_passed = test_passed
        ts_print(f"test_passed={test_passed}, "
                 f"ci_result={result.get('ci_result')}")
        if not test_passed:
            # Fix-mode contract (same as the local path): test_errors =
            # [detail file, result json] so the adapter's fix round reads
            # the per-test error details directly.
            tests_dir = WORKSPACE_DIR / STEPS_DIR / str(step_id) / "tests"
            summary_log = tests_dir / \
                f"round-{self.state.retry_count}-result.json"
            detail_file = build_test_errors_detail(
                result.get("suite_results", {}), self.state.retry_count,
                tests_dir, summary_log)
            self.state.test_errors = (
                [str(detail_file), str(summary_log)] if detail_file
                else [str(summary_log)])
        return test_passed

    def _run_e2e_test(self):
        step = self.state.steps[self.state.current_step]
        step_id = step["id"]
        ts_print(f"run_e2e_test: {step_id} round={self.state.retry_count}")

        if os.getenv("SKIP_E2E_TEST", "false").lower() == "true":
            ts_print(f"[run_e2e_test] SKIP_E2E_TEST=true, treating as passed")
            return True

        changed = [f for f in (self.state.changed_files or []) if f]
        if not _has_source_changes(changed):
            self.state.last_step_e2e_passed = False
            ts_print(f"[run_e2e_test] {step_id}: only non-source changes "
                     f"({changed or '(none)'}), skipping e2e")
            return True

        # Same rule as main's per-step e2e — a step with source changes must
        # be verified before commit — but executed on the external A2/A3
        # runners: the accumulated patch (prior commits + this step's
        # working-tree changes) is pushed to the signal branch, dispatched
        # as an exec round, and this flow waits for that run's artifacts
        # before deciding commit / adapter-fix / revert.
        return self._run_external_step_e2e(changed)

    def _fill_unattributed_analysis(self, unattributed: list[str],
                                     accumulated_patch_path: Path) -> list[dict]:
        """Invoke description-fill agent to analyze unattributed files.

        Returns a list of dicts (same shape as ``step_items`` entries) with
        ``files``, ``cause``, ``change``, ``upstream_links`` populated by the
        agent.  Returns an empty list if the agent fails or produces no
        parseable output — caller falls back to a catch-all row.
        """
        if not unattributed:
            return []
        import tempfile
        # Write unattributed file list + concatenate existing step summaries
        # for context.  Use a dedicated step_dir so the agent's step_summary.md
        # output doesn't clobber real step summaries.
        fill_dir = Path(tempfile.mkdtemp(prefix="description_fill_"))
        try:
            unattributed_set = set(unattributed)
            unattributed_files_path = fill_dir / "unattributed_files.txt"
            unattributed_files_path.write_text(
                "\n".join(unattributed) + "\n", encoding="utf-8")
            # Build a FILTERED patch containing only the unattributed files'
            # diffs.  Passing the full accumulated patch wastes tokens on files
            # the agent doesn't need to analyze (those already attributed to
            # steps).  If filtering fails, fall back to the full patch.
            filtered_patch_path = fill_dir / "unattributed.patch"
            try:
                full_patch = accumulated_patch_path.read_text(encoding="utf-8")
                filtered_lines: list[str] = []
                in_hunk = False
                current_file: str = ""
                for line in full_patch.splitlines():
                    if line.startswith("diff --git a/"):
                        # Start of a new file's diff.  Extract path and decide
                        # whether to include this section.
                        parts = line.split()
                        current_file = parts[-1][2:] if len(parts) >= 4 else ""
                        in_hunk = current_file in unattributed_set
                    if in_hunk:
                        filtered_lines.append(line)
                filtered_patch_path.write_text(
                    "\n".join(filtered_lines) + "\n", encoding="utf-8")
                patch_for_agent = filtered_patch_path
                ts_print(f"[generate_final_post] filtered patch: "
                         f"{len(filtered_lines)} lines for {len(unattributed)} "
                         f"unattributed files (full patch was "
                         f"{len(full_patch.splitlines())} lines)")
            except Exception as e:
                ts_print(f"[generate_final_post] patch filtering failed ({e}), "
                         f"using full accumulated patch")
                patch_for_agent = accumulated_patch_path
            # Concatenate all existing step summaries as previous context.
            prev_summaries: list[str] = []
            for i in range(self.state.current_step):
                s = self.state.steps[i]
                ssp = WORKSPACE_DIR / STEPS_DIR / s["id"] / EACH_STEP_SUMMARY_FILE
                if ssp.exists():
                    prev_summaries.append(
                        f"--- {s['id']} ---\n"
                        + ssp.read_text(encoding="utf-8"))
            prev_summary_path = fill_dir / "previous_summaries.md"
            prev_summary_path.write_text(
                "\n\n".join(prev_summaries) or "(no prior summaries)",
                encoding="utf-8")
            release_tag = self.state.release_tag or "0.0.0"
            ts_print(f"\n[generate_final_post] invoking description-fill agent "
                     f"({len(unattributed)} files, release_tag={release_tag})")
            adapt_result = run_opencode_adapter({
                "role": "description-fill",
                "step_id": "unattributed",
                "step_dir": str(fill_dir),
                "ascend_path": str(self.state.vllm_ascend_path),
                "vllm_path": str(self.state.vllm_path),
                "patch_path": str(patch_for_agent),
                "changed_files_path": str(unattributed_files_path),
                "previous_step_summary_path": str(prev_summary_path),
                "release_tag": release_tag,
                "is_last_step": "true",
                "mode": "description-fill",
            })
            out_summary = fill_dir / EACH_STEP_SUMMARY_FILE
            if not out_summary.exists():
                ts_print("[generate_final_post] description-fill produced no "
                         f"step_summary.md at {out_summary}")
                return []
            out_text = out_summary.read_text(encoding="utf-8")
            ts_print(f"[generate_final_post] description-fill output: "
                     f"{len(out_text.splitlines())} lines")
            # Parse each `- unattributed-N: Adapted — <files>` entry.
            items = self._parse_unattributed_entries(out_text)
            if items:
                ts_print(f"\n[generate_final_post] description-fill produced "
                         f"{len(items)} analysis entries for "
                         f"{sum(len(it['files']) for it in items)} files")
            else:
                ts_print("[generate_final_post] description-fill produced no "
                         "parseable entries")
            return items
        except Exception as e:
            ts_print(f"[generate_final_post] description-fill agent failed: {e}")
            return []
        finally:
            shutil.rmtree(fill_dir, ignore_errors=True)

    def _parse_unattributed_entries(self, summary_text: str) -> list[dict]:
        """Parse `- unattributed-N: Adapted — <files>` entries from the
        description-fill agent's output.

        Returns list of dicts: ``{files, cause, change, upstream_links}``.
        Mirrors the per-step parser in ``generate_final_post`` but keyed on
        ``unattributed-N`` headers instead of ``step-N``.
        """
        commit_url = "https://github.com/vllm-project/vllm/commit"
        items: list[dict] = []
        # Match `- unattributed-N: Adapted — ...` headers (N is any digit).
        header_re = re.compile(r"^- unattributed-\d+:\s*Adapted\s*—\s*(.*)$")
        # Split on header lines — each section starts with a header.
        sections: list[tuple[str, list[str]]] = []
        current_header: str = ""
        current_lines: list[str] = []
        for line in summary_text.splitlines():
            m = header_re.match(line.strip())
            if m:
                if current_header:
                    sections.append((current_header, current_lines))
                current_header = m.group(1)
                current_lines = []
            elif current_header:
                current_lines.append(line)
        if current_header:
            sections.append((current_header, current_lines))
        for header_tail, body_lines in sections:
            files = list(re.findall(r"`([^`]+)`", header_tail))
            if not files:
                continue
            cause = ""
            change = ""
            upstream_links: list[str] = []
            collecting = ""
            parts: list[str] = []
            for dline in body_lines:
                dl = dline.strip()
                if dl.startswith("Cause:"):
                    if collecting == "change":
                        change = " ".join(parts)
                    collecting = "cause"
                    parts = [dl.removeprefix("Cause:").strip()]
                    continue
                if dl.startswith("Change:"):
                    if collecting == "cause":
                        cause = " ".join(parts)
                    collecting = "change"
                    parts = [dl.removeprefix("Change:").strip()]
                    continue
                if dl.startswith("Upstream source:") or dl.startswith("Upstream commit:"):
                    m = re.search(r"\[([^\]]+)\]\(([^\)]+)\)", dl)
                    if m:
                        upstream_links.append(f"[{m.group(1)[:8]}]({m.group(2)})")
                    else:
                        sha = dl.split(":", 1)[1].strip()
                        if sha:
                            upstream_links.append(f"[{sha[:8]}]({commit_url}/{sha})")
                    continue
                if collecting and parts and dline.startswith(("  ", "\t")):
                    parts.append(dl)
            if collecting == "cause":
                cause = " ".join(parts)
            elif collecting == "change":
                change = " ".join(parts)
            items.append({
                "files": files,
                "cause": cause,
                "change": change,
                "upstream_links": upstream_links,
            })
        return items

    def generate_final_post(self):
        # The last successful step's patch is accumulated: git diff HEAD after all
        # successful adaptations. Prefer its accumulated summary, and fall back to
        # concatenating available step summaries if the last one is missing.
        # Squash per-step checkpoint commits into one so the PR always has a
        # single commit.  Must run BEFORE the current_step==0 check because
        # old runs may have left step commits on a reused branch — even if
        # this run adapted zero steps, the branch may still have stale commits.
        ascend_path = Path(self.state.vllm_ascend_path)
        step_count = subprocess.run(
            ["git", "rev-list", "--count",
             f"{self.state.original_ascend_ref}..HEAD"],
            cwd=str(ascend_path), capture_output=True, text=True,
        )
        if (step_count.returncode == 0
                and int(step_count.stdout.strip() or "0") > 1):
            run_git(ascend_path, "reset", "--soft",
                    self.state.original_ascend_ref)
            run_git(ascend_path, "add", "-A")
            target = self.state.target_commit or self.state.cur_vllm_commit
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            commit_msg = (
                f"main2main: sync vllm upstream "
                f"({self.state.base_commit[:8]}...{target[:8]}) [{ts}]"
            )
            run_git(ascend_path, "commit", "-s", "-m", commit_msg)
            ts_print(f"\n[generate_final_post] Squashed step commits into: "
                     f"{commit_msg}")
        else:
            ts_print("[generate_final_post] No step commits to squash (branch at baseline)")

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
            # No PR is created for a zero-success run (flow.run skips the
            # push) — a PR would carry only a "failed" description and the
            # last-attempted (broken) diff.  Do NOT copy that patch into
            # final_target.patch.
            return

        last_step = self.state.steps[self.state.current_step - 1]
        step_dir = WORKSPACE_DIR / STEPS_DIR / last_step["id"]
        final_summary_path = WORKSPACE_DIR / FINAL_SUMMARY_FILE
        step_patch = step_dir / EACH_STEP_TARGET_PATCH_FILE
        # Prefer the gate-regenerated accumulated patch (includes format/mypy
        # fixes made by the final quality gate).  The step patch was captured
        # BEFORE the gate, so it lacks those fixes.
        gate_patch = WORKSPACE_DIR / "gate_final_patch"
        if gate_patch.exists() and gate_patch.stat().st_size > 0:
            shutil.copy2(gate_patch, WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE)
            ts_print("\n[generate_final_post] Using gate-regenerated patch (with gate fixes) as final_target.patch")
        elif step_patch.exists():
            # Fall back to the last step's patch — but verify it has real
            # files beyond .github/vllm-main-verified.commit.  When the final
            # quality gate FAILED (e.g. 131 UT env failures in PR #13657),
            # gate_final_patch is never written; and if the last step was a
            # noop, its step_target.patch only contains the tracking file
            # (`git diff HEAD` after commits = uncommitted delta only).
            # Copying that produces an EMPTY file list in the PR description.
            # Detect this and regenerate the true accumulated diff from git.
            step_patch_text = step_patch.read_text(encoding="utf-8")
            if _extract_diff_files(step_patch_text):
                shutil.copy2(step_patch, WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE)
            else:
                ts_print("[generate_final_post] last step patch has no real "
                         "files — regenerating accumulated diff from git "
                         "(gate_final_patch missing)")
                subprocess.run(["git", "add", "-N", "."], cwd=str(ascend_path),
                               capture_output=True)
                accumulated_patch = run_git(
                    ascend_path, "diff", self.state.original_ascend_ref)
                (WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE).write_text(
                    accumulated_patch, encoding="utf-8")
                ts_print(f"[generate_final_post] regenerated accumulated diff "
                         f"({len(accumulated_patch.splitlines())} lines) as "
                         f"final_target.patch")

        # Build PR body: concise numbered list matching PR #5595 style.
        # Each item: "Adapt <files> due to [commit](link) — <cause>"
        commit_url = "https://github.com/vllm-project/vllm/commit"

        # Source of truth for the file list: the accumulated patch
        # (gate_final_patch or fall-back step patch) copied to
        # FINAL_TARGET_PATCH_FILE above.  Per-step EACH_STEP_TARGET_PATCH_FILE
        # is incremental (captured via `git diff HEAD` at adapt time), so when
        # step-1 is retried multiple times only the last retry's files survive
        # — earlier retries' files would be lost from the description.  The
        # accumulated patch captures all changes since original_ascend_ref.
        accumulated_patch_path = WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE
        accumulated_files: list[str] = []
        if accumulated_patch_path.exists():
            accumulated_files = _extract_diff_files(
                accumulated_patch_path.read_text(encoding="utf-8"))
            ts_print(f"\n[generate_final_post] accumulated patch has "
                     f"{len(accumulated_files)} file(s) for PR description")
        else:
            ts_print("[generate_final_post] WARNING: accumulated patch file "
                     "missing — PR description will have no file list")

        # Collect per-step: adapted files, cause, and triggering commit.
        # Files per step are attributed via the step_summary.md header
        # ("- {step_id}: Adapted — <files>") plus backtick-quoted paths in the
        # Change: field.  Files in the accumulated patch not mentioned in any
        # step summary are surfaced in a separate "Unattributed" row.
        step_items: list[dict] = []
        seen_files: set[str] = set()
        for i in range(self.state.current_step):
            s = self.state.steps[i]
            cause = ""
            change = ""
            upstream_links: list[str] = []
            is_noop = False
            ssp = WORKSPACE_DIR / STEPS_DIR / s["id"] / EACH_STEP_SUMMARY_FILE
            ssp_text = ""
            if ssp.exists():
                ssp_text = ssp.read_text(encoding="utf-8")
                # No-op step: "- step-N: No-op — <reason>" (no adaptation).
                # Such steps only confirm the upstream range has no
                # vllm-ascend impact — collapse them into one summary row
                # instead of an empty table line.
                if re.search(rf"^- {re.escape(s['id'])}:\s*No-op", ssp_text, re.M):
                    is_noop = True
                in_our_step = False
                parts: list[str] = []
                collecting: str = ""  # "cause" or "change"
                for dline in ssp_text.strip().splitlines():
                    dl = dline.strip()
                    # Per-step section: e.g. "- step-2: Adapted — ..."
                    if dl.startswith(f"- {s['id']}:"):
                        in_our_step = True
                        continue
                    # Next step entry starts → stop collecting
                    if in_our_step and dl.startswith("- step-") and ":" in dl:
                        break
                    if not in_our_step:
                        continue
                    if dl.startswith("Cause:"):
                        collecting = "cause"
                        parts = [dl.removeprefix("Cause:").strip()]
                        continue
                    if dl.startswith("Change:"):
                        if collecting == "cause":
                            cause = " ".join(parts)
                        collecting = "change"
                        parts = [dl.removeprefix("Change:").strip()]
                        continue
                    if dl.startswith("Upstream source:") or dl.startswith("Upstream commit:"):
                        # Parse: "Upstream source: [sha](url)" or "Upstream commit: <sha>"
                        m = re.search(r'\[([^\]]+)\]\(([^\)]+)\)', dl)
                        if m:
                            upstream_links.append(f"[{m.group(1)[:8]}]({m.group(2)})")
                        else:
                            # Plain SHA format: "Upstream commit: <sha>".
                            # Only build a link for a real sha — the agent
                            # sometimes writes "(unknown ...)" here, which
                            # must not become a malformed markdown link.
                            sha = dl.split(":", 1)[1].strip()
                            if re.fullmatch(r"[0-9a-fA-F]{7,40}", sha):
                                upstream_links.append(
                                    f"[{sha[:8]}]({commit_url}/{sha})")
                        continue
                    # Continuation lines (indented with 2+ spaces or tab).
                    # Only match indented lines — the old fallback (dl and not
                    # dl.startswith("-")) was too broad and caught unrelated
                    # lines like "Upstream commit:" into the cause/change text.
                    if collecting and parts and dline.startswith(("  ", "\t")):
                        parts.append(dl)
                if collecting == "cause":
                    cause = " ".join(parts)
                elif collecting == "change":
                    change = " ".join(parts)
            # Attribute accumulated files to this step via the summary header
            # and the Change: field's backtick-quoted paths.  Falls back to
            # mentioning all accumulated files for the step if the adapter
            # didn't follow the SKILL.md header format but the step is the
            # only one (single-step flow).
            header_files = _parse_summary_files(ssp_text, s["id"])
            change_files = set(re.findall(r"`([^`]+)`", change))
            # The adapter often names the touched files inline in the
            # Change/Cause text without backticks — extract vllm_ascend/
            # and tests/ paths so the Files column isn't "—" and the
            # completeness guard doesn't re-surface them as unattributed.
            text_files = set(re.findall(
                r"(?:[\w./-]+/)?(?:vllm_ascend|tests)/[\w./-]+\.py",
                f"{cause} {change}"))
            mentioned = header_files | change_files | text_files
            if mentioned:
                step_files = [f for f in accumulated_files if f in mentioned]
            else:
                # Fallback: the adapter didn't name files in the summary
                # (no backtick paths, no vllm_ascend/...py mentions).
                # Extract from the step's own patch (EACH_STEP_TARGET_PATCH_FILE,
                # captured via `git diff HEAD` at adapt time) so the Files
                # column isn't "—" when vllm-ascend code was actually modified
                # (PR #14778: step summary said "guarded both methods" without
                # naming the files → Files column was empty).
                step_patch_path = (WORKSPACE_DIR / STEPS_DIR / s["id"]
                                   / EACH_STEP_TARGET_PATCH_FILE)
                if step_patch_path.exists():
                    step_patch_files = _extract_diff_files(
                        step_patch_path.read_text(encoding="utf-8"))
                    step_files = [f for f in step_patch_files
                                  if f in accumulated_files]
                else:
                    step_files = []
            seen_files.update(step_files)
            if is_noop and not step_files and not cause and not change:
                # No-op step: no vllm-ascend impact — exclude from the PR
                # description entirely (only real adaptations appear).
                continue
            step_items.append({
                "files": step_files,
                "commit": s["end_commit"][:8],
                "cause": cause,
                "change": change,
                "upstream_links": upstream_links,
            })

        # Files changed in the accumulated patch but not mentioned in any
        # step summary.  These are real adaptations (or sync-commit changes)
        # that the adapter didn't attribute to a step.  Invoke the
        # description-fill agent to analyze each one and produce proper
        # Cause/Change entries, so the PR description has full analysis
        # (not just a catch-all file list).
        unattributed = [f for f in accumulated_files if f not in seen_files]
        unattributed_items: list[dict] = []
        if unattributed:
            ts_print(f"[generate_final_post] {len(unattributed)} file(s) in "
                     "accumulated patch but not mentioned in any step summary — "
                     "invoking description-fill agent to analyze them")
            unattributed_items = self._fill_unattributed_analysis(
                unattributed, accumulated_patch_path)

        # Get the commit date of the target vllm commit for the PR description.

        # Get the commit date of the target vllm commit for the PR description.
        target = self.state.target_commit or self.state.cur_vllm_commit
        commit_date = datetime.now()
        if target:
            r = subprocess.run(
                ["git", "log", "-1", "--format=%ct", target],
                cwd=self.state.vllm_path, capture_output=True, text=True,
            )
            if r.returncode == 0 and r.stdout.strip():
                commit_date = datetime.fromtimestamp(int(r.stdout.strip()))

        parts = [
            "### What this PR does / why we need it?",
            "",
            f"Adapt vllm-ascend to vLLM main commits up to {commit_date.strftime('%B %d')}.",
            "",
            "### Changes",
            "",
            "| Files | Upstream vLLM change | vllm-ascend adaptation |",
            "|-------|---------------------|------------------------|",
        ]
        # Table rows must never contain empty cells — every row carries
        # all three columns (Files / Upstream vLLM change / vllm-ascend
        # adaptation).  Missing values get a "—" placeholder.
        for item in step_items:
            files_str = "<br>".join(f"`{f}`" for f in item["files"]) or "—"
            # Upstream column: PR/commit links + cause
            links = item.get("upstream_links") or []
            if not links:
                links = [f"[{item['commit'][:8]}]({commit_url}/{item['commit']})"]
            upstream = " · ".join(links)
            if item.get("cause"):
                upstream = f"{upstream} — {item['cause']}"
            if not item.get("cause"):
                upstream = upstream or "—"
            # Adaptation column: never fall back to cause text — the two
            # columns serve different purposes.  If the adapter didn't write a
            # Change, show the files touched instead.
            adapt = item.get("change") or "—"
            parts.append(f"| {files_str} | {upstream} | {adapt} |")
        # Unattributed rows: each has full Cause/Change from the
        # description-fill agent.  Falls back to a catch-all row only if the
        # agent failed to produce analysis (e.g., opencode unavailable).
        for item in unattributed_items:
            files_str = "<br>".join(f"`{f}`" for f in item["files"]) or "—"
            links = item.get("upstream_links") or []
            if links:
                upstream = " · ".join(links)
                if item.get("cause"):
                    upstream = f"{upstream} — {item['cause']}"
            else:
                upstream = item.get("cause") or "(unattributed)"
            adapt = item.get("change") or "—"
            parts.append(f"| {files_str} | {upstream} | {adapt} |")
        # Completeness guard: EVERY file in the accumulated patch must appear
        # in the description.  Files covered by neither step attribution nor
        # the description-fill agent (e.g. agent partially failed) would
        # otherwise vanish silently — surface them in a catch-all row so
        # reviewers still see the full scope.
        covered: set[str] = set()
        for item in step_items:
            covered.update(item["files"])
        for item in unattributed_items:
            covered.update(item["files"])
        missing = [f for f in accumulated_files if f not in covered]
        if missing:
            files_str = "<br>".join(f"`{f}`" for f in missing) or "—"
            parts.append(
                f"| {files_str} | (unattributed) "
                f"| see accumulated patch — description-fill agent produced no analysis |"
            )
        if not step_items and not accumulated_files:
            # All steps were no-op (upstream range had no vllm-ascend impact)
            # and the accumulated patch has no real files — the table would be
            # header-only, which renders as a broken empty PR description.
            parts.append("| (no vllm-ascend adaptation in this range) | — | — |")
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
        labels_str = os.getenv("PR_LABELS", "ready-all")
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
            base_ref=self.state.original_ascend_ref,
        )
