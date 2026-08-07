
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
from main2main_flow.scripts.utils.lessons import persist_lessons, submit_step_lesson
from main2main_flow.scripts.utils.push_to_github import push_and_create_pr, resolve_squash_baseline
from main2main_flow.scripts.utils.run_tests import run_tests
from main2main_flow.scripts.utils.update_commit_reference import run_update
from main2main_flow.scripts.utils.final_quality_gate import run_final_quality_gate
from main2main_flow.scripts.utils.utils import (
    UpgradeCompleted, UpgradeFailed,
    HasCommit, HasNoCommit, resolve_path, WORKSPACE_DIR, DETECT_FILE, STEPS_FILE, FINAL_SUMMARY_FILE, FINAL_TARGET_PATCH_FILE,
    STEPS_DIR, VLLM_GIT_PATCH_FILE, VLLM_GIT_CHANGED_FILES, PRE_CI_CHECK_FILE,
    EACH_STEP_SUMMARY_FILE, EACH_STEP_TARGET_PATCH_FILE, EACH_STEP_CODE_STRUCTURE_GUIDE_FILE,
    FINAL_CODE_STRUCTURE_GUIDE_FILE, run_git, ts_print
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
    read the cumulative patch's file list as the source of truth — per-step
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
    # on the cumulative state (all prior steps' commits + this step's patch),
    # so it subsumes earlier steps.  When the last step skipped its e2e (or
    # failed), the cumulative state is unvalidated and the final quality gate
    # must run the regression e2e — the agent's no-op judgment can be wrong.
    last_step_e2e_passed: bool = False

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
        # Inject the full review-lessons.md: §1-8 give context (classic
        # examples) for each checklist item in §9, so the reviewer can
        # pattern-match, not just tick boxes.
        checklist = lessons

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

    def run(self, inputs: dict | None = None):
        if inputs:
            for k, v in inputs.items():
                setattr(self.state, k, v)
        self.initialize()
        signal = self.analyze_commit_and_plan_step()
        if signal == HasNoCommit:
            self.has_no_commit()
            return
        self.process_steps()
        self.generate_final_post()
        # Persist adaptation lessons (E2E fix rounds) back to vllm-report
        # before push — the clone is recreated every run, so unsaved
        # lessons would be lost.
        persist_lessons(self.state.vllm_report_path)
        self._cleanup_release_worktree()
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
            # the repo or the PR diff.
            exclude_file = Path(self.state.vllm_ascend_path) / ".git" / "info" / "exclude"
            try:
                exclude_content = exclude_file.read_text(encoding="utf-8")
                if "opencode.json" not in exclude_content:
                    exclude_file.write_text(
                        exclude_content.rstrip() + "\nopencode.json\n", encoding="utf-8"
                    )
            except OSError:
                # .git/info/exclude not writable - append to .gitignore instead
                # so opencode.json still doesn't get committed into the PR.
                gitignore_path = Path(self.state.vllm_ascend_path) / ".gitignore"
                try:
                    gi_content = gitignore_path.read_text(encoding="utf-8")
                    if "opencode.json" not in gi_content:
                        gitignore_path.write_text(
                            gi_content.rstrip() + "\nopencode.json\n", encoding="utf-8"
                        )
                        ts_print("[init] added opencode.json to .gitignore (exclude not writable)")
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

        # Final quality gate: format + mypy on the cumulative diff, before push.
        # Failures enter adapter-fix mode (max 3 rounds); each fix re-runs e2e
        # to confirm functional correctness wasn't broken by format/mypy edits.
        if self.state.final_status == UpgradeCompleted:
            if not self._final_quality_gate():
                self.state.final_status = UpgradeFailed

    def _capture_step_patch(self, ascend_path: str, step_dir: Path,
                            step_id: str) -> None:
        """Capture the working-tree diff as step_target.patch and set state.

        Used by the no-adaptation branches (SKIP_AI_ANALYSIS / empty upstream
        patch) where cur_patch_path must point at an existing file - run_tests'
        setup_env exits if the patch is missing.
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
        for attempt in range(1, 4):
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
                    # judgment may be wrong) or failed — the cumulative state
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
                ts_print(f"\n[final_quality_gate] PASSED (attempt {attempt})")
                # Regenerate the cumulative patch from the CURRENT working
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
                "vllm_report_context": "",
            }, session_id=self.state.session_id)
            if adapt_result.session_id:
                self.state.session_id = adapt_result.session_id

        ts_print("\n[final_quality_gate] exhausted 3 fix rounds, still failing")
        # Revert the last failed fix round so it doesn't leak into
        # generate_final_post's squash via `git add -A` and get pushed
        # (KEEP_BRANCH mode does add -A + amend at push time).
        self._revert_working_tree("final quality gate exhausted 3 fix rounds")
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

        # Use the CUMULATIVE changed files (baseline -> final tree) for test
        # selection.  The last step's changed_files only covers that step, but
        # gate format/mypy fixes can touch ANY file in the repo (mypy runs the
        # whole tree), so the regression e2e must cover all accumulated changes.
        cumulative_files = run_git(
            ascend_path, "diff", "--name-only", self.state.original_ascend_ref
        ).strip().splitlines()
        cumulative_files = [f for f in cumulative_files if f]

        # Save and override env vars to skip vllm reinstall + preserve ascend tree.
        saved_skip_pip = os.environ.get("SKIP_PIP_INSTALL", "")
        saved_keep_branch = os.environ.get("MAIN2MAIN_KEEP_BRANCH", "")
        os.environ["SKIP_PIP_INSTALL"] = "true"
        os.environ["MAIN2MAIN_KEEP_BRANCH"] = "true"
        try:
            result = run_tests(
                vllm_path=vllm_path,
                vllm_commit=self.state.cur_vllm_commit,
                ascend_path=ascend_path,
                ascend_commit=self.state.cur_ascend_commit,
                patch_path=str(patch_path),
                step_id=step_id,
                select_by_files=cumulative_files or None,
                test_cases=_resolve_test_cases(),
                remote=os.getenv("MAIN2MAIN_RUN_TESTS_REMOTE") or None,
                round_number=0,
                log_dir=str(WORKSPACE_DIR / STEPS_DIR),
            )
        finally:
            if saved_skip_pip:
                os.environ["SKIP_PIP_INSTALL"] = saved_skip_pip
            else:
                os.environ.pop("SKIP_PIP_INSTALL", None)
            if saved_keep_branch:
                os.environ["MAIN2MAIN_KEEP_BRANCH"] = saved_keep_branch
            else:
                os.environ.pop("MAIN2MAIN_KEEP_BRANCH", None)

        test_passed = result.get("can_commit", False)
        ts_print(f"\n[final_quality_gate] regression e2e: {'PASSED' if test_passed else 'FAILED'}")
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

        changed = [f for f in (self.state.changed_files or []) if f]
        if not _has_source_changes(changed):
            self.state.last_step_e2e_passed = False
            ts_print(f"[run_e2e_test] {step_id}: only non-source changes "
                     f"({changed or '(none)'}), skipping e2e")
            return True

        ts_print(f"The adaptation patch is at: {self.state.cur_patch_path}")
        result = run_tests(
            vllm_path=self.state.vllm_path,
            vllm_commit=self.state.cur_vllm_commit,
            ascend_path=self.state.vllm_ascend_path,
            ascend_commit=self.state.cur_ascend_commit,
            patch_path=self.state.cur_patch_path or None,
            step_id=step_id,
            select_by_files=changed,
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
        summary_log_path = Path(summary_log)
        if not summary_log_path.exists():
            summary_log_path.write_text(
                json.dumps(result, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
        ts_print(f"test_passed={test_passed}, ci_result={result.get('ci_result')}")
        self.state.last_step_e2e_passed = test_passed

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

    def _fill_unattributed_analysis(self, unattributed: list[str],
                                     cumulative_patch_path: Path) -> list[dict]:
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
            # diffs.  Passing the full cumulative patch wastes tokens on files
            # the agent doesn't need to analyze (those already attributed to
            # steps).  If filtering fails, fall back to the full patch.
            filtered_patch_path = fill_dir / "unattributed.patch"
            try:
                full_patch = cumulative_patch_path.read_text(encoding="utf-8")
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
                         f"using full cumulative patch")
                patch_for_agent = cumulative_patch_path
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
        # The last successful step's patch is cumulative: git diff HEAD after all
        # successful adaptations. Prefer its cumulative summary, and fall back to
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
            # Try to copy the last attempted step's patch so push_to_github
            # can still create a PR with the best-effort diff.
            if self.state.total_steps > 0:
                last_attempted = self.state.steps[-1]
                last_step_dir = WORKSPACE_DIR / STEPS_DIR / last_attempted["id"]
                last_patch = last_step_dir / EACH_STEP_TARGET_PATCH_FILE
                if last_patch.exists():
                    shutil.copy2(last_patch, WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE)
                    ts_print("[generate_final_post] Copied last attempted step's patch as final_target.patch")
            return

        last_step = self.state.steps[self.state.current_step - 1]
        step_dir = WORKSPACE_DIR / STEPS_DIR / last_step["id"]
        final_summary_path = WORKSPACE_DIR / FINAL_SUMMARY_FILE
        step_patch = step_dir / EACH_STEP_TARGET_PATCH_FILE
        # Prefer the gate-regenerated cumulative patch (includes format/mypy
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
            # Detect this and regenerate the true cumulative diff from git.
            step_patch_text = step_patch.read_text(encoding="utf-8")
            if _extract_diff_files(step_patch_text):
                shutil.copy2(step_patch, WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE)
            else:
                ts_print("[generate_final_post] last step patch has no real "
                         "files — regenerating cumulative diff from git "
                         "(gate_final_patch missing)")
                subprocess.run(["git", "add", "-N", "."], cwd=str(ascend_path),
                               capture_output=True)
                cumulative_patch = run_git(
                    ascend_path, "diff", self.state.original_ascend_ref)
                (WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE).write_text(
                    cumulative_patch, encoding="utf-8")
                ts_print(f"[generate_final_post] regenerated cumulative diff "
                         f"({len(cumulative_patch.splitlines())} lines) as "
                         f"final_target.patch")

        # Build PR body: concise numbered list matching PR #5595 style.
        # Each item: "Adapt <files> due to [commit](link) — <cause>"
        commit_url = "https://github.com/vllm-project/vllm/commit"

        # Source of truth for the file list: the cumulative patch
        # (gate_final_patch or fall-back step patch) copied to
        # FINAL_TARGET_PATCH_FILE above.  Per-step EACH_STEP_TARGET_PATCH_FILE
        # is incremental (captured via `git diff HEAD` at adapt time), so when
        # step-1 is retried multiple times only the last retry's files survive
        # — earlier retries' files would be lost from the description.  The
        # cumulative patch captures all changes since original_ascend_ref.
        cumulative_patch_path = WORKSPACE_DIR / FINAL_TARGET_PATCH_FILE
        cumulative_files: list[str] = []
        if cumulative_patch_path.exists():
            cumulative_files = _extract_diff_files(
                cumulative_patch_path.read_text(encoding="utf-8"))
            ts_print(f"\n[generate_final_post] cumulative patch has "
                     f"{len(cumulative_files)} file(s) for PR description")
        else:
            ts_print("[generate_final_post] WARNING: cumulative patch file "
                     "missing — PR description will have no file list")

        # Collect per-step: adapted files, cause, and triggering commit.
        # Files per step are attributed via the step_summary.md header
        # ("- {step_id}: Adapted — <files>") plus backtick-quoted paths in the
        # Change: field.  Files in the cumulative patch not mentioned in any
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
            # Attribute cumulative files to this step via the summary header
            # and the Change: field's backtick-quoted paths.  Falls back to
            # mentioning all cumulative files for the step if the adapter
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
                step_files = [f for f in cumulative_files if f in mentioned]
            else:
                # No file attribution from summary — leave empty; cumulative
                # files will surface in the Unattributed row below.
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

        # Files changed in the cumulative patch but not mentioned in any
        # step summary.  These are real adaptations (or sync-commit changes)
        # that the adapter didn't attribute to a step.  Invoke the
        # description-fill agent to analyze each one and produce proper
        # Cause/Change entries, so the PR description has full analysis
        # (not just a catch-all file list).
        unattributed = [f for f in cumulative_files if f not in seen_files]
        unattributed_items: list[dict] = []
        if unattributed:
            ts_print(f"[generate_final_post] {len(unattributed)} file(s) in "
                     "cumulative patch but not mentioned in any step summary — "
                     "invoking description-fill agent to analyze them")
            unattributed_items = self._fill_unattributed_analysis(
                unattributed, cumulative_patch_path)

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
        # Completeness guard: EVERY file in the cumulative patch must appear
        # in the description.  Files covered by neither step attribution nor
        # the description-fill agent (e.g. agent partially failed) would
        # otherwise vanish silently — surface them in a catch-all row so
        # reviewers still see the full scope.
        covered: set[str] = set()
        for item in step_items:
            covered.update(item["files"])
        for item in unattributed_items:
            covered.update(item["files"])
        missing = [f for f in cumulative_files if f not in covered]
        if missing:
            files_str = "<br>".join(f"`{f}`" for f in missing) or "—"
            parts.append(
                f"| {files_str} | (unattributed) "
                f"| see cumulative patch — description-fill agent produced no analysis |"
            )
        if not step_items and not cumulative_files:
            # All steps were no-op (upstream range had no vllm-ascend impact)
            # and the cumulative patch has no real files — the table would be
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
            base_ref=self.state.original_ascend_ref,
        )
