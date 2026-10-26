# SPDX-License-Identifier: Apache-2.0
"""CPU-only composition tests for the production detection/QA call path."""

import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

from main2main_flow.interface_report import runtime
from main2main_flow.interface_report.handoff import load_report
from main2main_flow.interface_report.run import git
from main2main_flow.scripts.utils.utils import INTERFACE_QA_REPORT_FILE, INTERFACE_RUN_DIR, INTERFACE_STATUS_FILE
import test_interface_scan as fixtures

with patch("shutil.which", return_value="opencode"):
    from main2main_flow.flow import Main2MainFlow


class RuntimeTests(unittest.TestCase):
    write = staticmethod(fixtures.InterfaceTests.write)
    commit = staticmethod(fixtures.InterfaceTests.commit)

    def setUp(self):
        fixtures.InterfaceTests.setUp(self)
        self.addCleanup(self.temp.cleanup)

    def scan(self):
        return runtime.scan_for_flow(
            self.up,
            self.down,
            self.old,
            self.new,
            self.output,
            cache_dir=self.cache,
        )

    def status(self):
        return json.loads((self.output / "run-status.json").read_text(encoding="utf-8"))

    def test_fixed_target_ignores_build_files_and_reuses_verified_report(self):
        # HEAD has a later compatible signature; detect's fixed target wins.
        self.write(self.up, "vllm/api.py", "def run(value, required=None):\n    return value\n")
        later = self.commit(self.up)
        self.write(self.down, "vllm_ascend/generated.py", "raise RuntimeError('do not import')\n")
        before = git(self.down, "status", "--porcelain")
        report = self.scan()
        self.assertEqual(self.status()["scan"], "completed")
        self.assertGreater(self.status()["summary"]["actionable_introduced_break"], 0)
        self.assertIn(self.new, report.read_text(encoding="utf-8"))
        self.assertIn("required", report.read_text(encoding="utf-8"))
        self.assertEqual(git(self.up, "rev-parse", "HEAD"), later)
        self.assertEqual(git(self.down, "status", "--porcelain"), before)
        (self.output / "scan.log").unlink()
        self.scan()
        self.assertEqual(self.status()["cache_status"], "hit")
        self.assertFalse((self.output / "scan.log").exists())
        self.assertEqual(self.status()["model_calls"], 0)

    def test_cumulative_baseline_is_used_and_invalidates_cache(self):
        self.scan()
        old_key = json.loads((self.output / "run-metadata.json").read_text())["cache_key"]
        self.write(
            self.down, "vllm_ascend/client.py", "from vllm.api import run\n\ndef invoke():\n    return run(1, 2)\n"
        )
        baseline = self.commit(self.down)
        self.scan()
        metadata = json.loads((self.output / "run-metadata.json").read_text())
        self.assertEqual(metadata["inputs"]["vllm_ascend_sha"], baseline)
        self.assertNotEqual(metadata["cache_key"], old_key)
        self.assertEqual(self.status()["summary"]["actionable_introduced_break"], 0)
        self.assertEqual(self.status()["cache_status"], "miss")

    def test_dirty_tracked_source_and_wrong_marker_fail_explicitly(self):
        self.scan()
        self.write(self.down, "vllm_ascend/client.py", "uncommitted = True\n")
        with self.assertRaisesRegex(ValueError, "Commit tracked source"):
            self.scan()
        self.assertEqual(self.status()["scan"], "failed")
        self.assertFalse((self.output / "qa-review.md").exists())
        git(self.down, "restore", "vllm_ascend/client.py")
        self.write(self.down, ".github/vllm-main-verified.commit", self.new + "\n")
        self.commit(self.down)
        with self.assertRaisesRegex(ValueError, "committed Ascend baseline"):
            self.scan()

    def test_legacy_conf_baseline_remains_supported(self):
        (self.down / ".github/vllm-main-verified.commit").unlink()
        self.write(self.down, "docs/source/conf.py", 'myst_substitutions = {"main_vllm_commit": "' + self.old + '"}\n')
        self.commit(self.down)
        self.assertTrue(self.scan().is_file())

    def test_engine_failure_leaves_failed_status_and_no_stale_qa_input(self):
        self.output.mkdir()
        (self.output / "qa-review.md").write_text("stale report", encoding="utf-8")
        with patch.object(runtime, "_scan_prepared", side_effect=subprocess.TimeoutExpired("engine", 2400)):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.scan()
        self.assertEqual(self.status()["scan"], "failed")
        self.assertFalse((self.output / "qa-review.md").exists())

    def test_original_qa_reuses_auto_report_across_retries(self):
        flow = Main2MainFlow()
        # A fresh analyze call clears a prior run's unavailable-scan state.
        flow.state.interface_scan_failed = True
        flow.state.vllm_path, flow.state.vllm_ascend_path = str(self.up), str(self.down)
        flow.state.target_commit = self.new
        workspace = self.root / "workspace"
        workspace.mkdir()
        step = {
            "id": "step-1",
            "index": 1,
            "commit_count": 1,
            "effective_lines": 2,
            "commits": [{"sha": self.new, "date": "2026-01-01"}],
            "start_commit": self.old,
            "end_commit": self.new,
            "upstream_patch": "",
            "changed_files": "vllm/api.py",
        }
        summary_file = self.root / "job-summary.md"
        calls = []

        def scan(*args):
            calls.append("scan")
            return runtime.scan_for_flow(*args, cache_dir=self.cache)

        def plan(*args, **kwargs):
            calls.append("plan")
            return {"steps": [step], "total_commits": 1}

        with (
            patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(summary_file)}),
            patch("main2main_flow.flow.WORKSPACE_DIR", workspace),
            patch("main2main_flow.flow.scan_for_flow", side_effect=scan) as scanner,
            patch("main2main_flow.flow.run_plan", side_effect=plan),
            patch("main2main_flow.flow.compute_base_ut_baseline"),
            patch("main2main_flow.flow.run_git"),
        ):
            self.assertEqual(flow.analyze_commit_and_plan_step(), "HasCommit")
            self.assertEqual(calls, ["scan", "plan"])
            scanner.assert_called_once()
            self.assertFalse(flow.state.interface_scan_failed)
        self.assertIn("## Main2Main step plan", summary_file.read_text(encoding="utf-8"))

        prompts = []
        step_dir = workspace / "steps/step-1"
        report = load_report(flow.state.interface_report)
        assessment = dict(report_sha256=report.sha256, step_id="step-1", start_commit=self.old, end_commit=self.new,
                          items=[dict(root_cause_id=root, decision="adapted", reason="required argument provided",
                                      evidence=["vllm/api.py:1", "vllm_ascend/client.py:4"]) for root in report.roots])
        (step_dir / "step_summary.md").write_text(
            "```interface-assessment\n" + json.dumps(assessment) + "\n```\n", encoding="utf-8")

        def review(prompt, **kwargs):
            prompts.append(prompt)
            (step_dir / "review.json").write_text(json.dumps({
                "verdict": "pass", "issues": [], "interface_report_sha256": report.sha256,
                "interface_step_id": "step-1", "interface_report_usage": [dict(
                    root_cause_id=root, judgment="agree", assessment="confirmed source contract",
                    evidence=["vllm/api.py:1", "vllm_ascend/client.py:4"]) for root in report.roots],
            }), encoding="utf-8")
            return "saved", "qa-session"

        # An unrelated environment override cannot replace this flow's report.
        with (
            patch.dict(os.environ, {"MAIN2MAIN_INTERFACE_REPORT": str(self.root / "missing.md")}),
            patch(
                "main2main_flow.flow.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, stdout="adaptation diff"),
            ),
            patch("main2main_flow.flow.run_opencode_review", side_effect=review) as qa,
        ):
            for retry in range(3):
                flow.state.retry_count = retry
                self.assertEqual(
                    flow._run_adapter_qa(
                        ascend_path=str(self.down),
                        vllm_path=str(self.up),
                        step_id="step-1",
                        step_dir=str(step_dir),
                        release_tag="0.1.0",
                    ),
                    ([], "qa-session"),
                )
            self.assertEqual(qa.call_count, 3)
        self.assertTrue(all(flow.state.interface_report in prompt for prompt in prompts))

    def test_no_change_skips_scan(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        flow = Main2MainFlow()
        flow.state.vllm_path, flow.state.vllm_ascend_path = str(self.up), str(self.down)
        result = {"base_commit": self.old, "target_commit": self.new, "compat_tag": ""}
        with (
            patch("main2main_flow.flow.WORKSPACE_DIR", workspace),
            patch("main2main_flow.flow.detect", return_value=(result, False)),
            patch("main2main_flow.flow.scan_for_flow") as scanner,
        ):
            self.assertEqual(flow.analyze_commit_and_plan_step(), "HasNoCommit")
            scanner.assert_not_called()

    def test_scan_failures_continue_planning_without_stale_report(self):
        for failure in (ValueError("bad scan input"), subprocess.TimeoutExpired("engine", 2400),
                        RuntimeError("engine crashed"), None):
            with self.subTest(failure=failure):
                workspace = self.root / "workspace"
                workspace.mkdir(exist_ok=True)
                scan_dir = workspace / INTERFACE_RUN_DIR
                scan_dir.mkdir(exist_ok=True)
                stale = scan_dir / INTERFACE_QA_REPORT_FILE
                stale.write_text("## abcdef0123456789 — stale\n", encoding="utf-8")
                flow = Main2MainFlow(vllm_path=str(self.up), vllm_ascend_path=str(self.down))
                flow.state.interface_report = str(stale)
                flow.state.interface_report_sha256 = "stale hash"
                step = dict(id="step-1", start_commit=self.old, end_commit=self.new,
                            upstream_patch="diff", changed_files="vllm_ascend/client.py",
                            index=1, commit_count=1, effective_lines=2,
                            commits=[{"sha": self.new, "date": "2026-01-01"}])
                summary = self.root / "job-summary.md"

                def scan(*args):
                    if failure is not None:
                        raise failure
                    # Successful execution with an invalid final handoff report
                    # is unavailable evidence too, not a blocker for adaptation.
                    stale.write_text("## abcdef0123456789 — a\n## abcdef0123456789 — b\n", encoding="utf-8")
                    return stale

                with (
                    patch.dict(os.environ, {"MAIN2MAIN_INTERFACE_REPORT": str(self.root / "missing.md"),
                                            "GITHUB_STEP_SUMMARY": str(summary)}),
                    patch("main2main_flow.flow.WORKSPACE_DIR", workspace),
                    patch("main2main_flow.flow.scan_for_flow", side_effect=scan) as scanner,
                    patch("main2main_flow.flow.run_plan", return_value={"steps": [step], "total_commits": 1}) as planner,
                    patch("main2main_flow.flow.compute_base_ut_baseline"),
                    patch("main2main_flow.flow.run_git"),
                ):
                    self.assertEqual(flow.analyze_commit_and_plan_step(), "HasCommit")
                    scanner.assert_called_once()
                    planner.assert_called_once()
                    self.assertEqual(flow.state.steps, [step])
                    self.assertEqual(flow.state.total_steps, 1)
                    self.assertTrue(flow.state.interface_scan_failed)
                    self.assertEqual(flow.state.interface_report, "")
                    self.assertEqual(flow.state.interface_report_sha256, "")
                    self.assertEqual(flow._interface_adapter_inputs(step)["interface_report"], "")
                    self.assertEqual(flow._interface_assessment("step-1", workspace / "steps/step-1"), (None, None))
                    self.assertFalse(stale.exists())
                    status = json.loads((scan_dir / INTERFACE_STATUS_FILE).read_text(encoding="utf-8"))
                    self.assertEqual(status["scan"], "failed")
                    self.assertEqual(status["adaptation"], "continued_without_interface_report")
                    self.assertIn("This is not a clean interface scan", summary.read_text(encoding="utf-8"))
                    self.assertTrue((workspace / "steps/step-1/upstream.patch").exists())

    def test_scan_diagnostic_write_failures_do_not_stop_planning(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        flow = Main2MainFlow(vllm_path=str(self.up), vllm_ascend_path=str(self.down))
        result = {"base_commit": self.old, "target_commit": self.new, "compat_tag": ""}
        original_write = Path.write_text

        def write(path, *args, **kwargs):
            if INTERFACE_RUN_DIR in path.parts:
                raise PermissionError("diagnostics unavailable")
            return original_write(path, *args, **kwargs)

        with (
            patch.dict(os.environ, {"GITHUB_STEP_SUMMARY": str(self.root / "missing-parent/summary.md")}),
            patch("main2main_flow.flow.WORKSPACE_DIR", workspace),
            patch("main2main_flow.flow.detect", return_value=(result, True)),
            patch("main2main_flow.flow.scan_for_flow", side_effect=PermissionError("scan unavailable")),
            patch("main2main_flow.flow.run_plan", return_value={"steps": [], "total_commits": 1}) as planner,
            patch.object(Path, "unlink", side_effect=PermissionError("cleanup unavailable")),
            patch.object(Path, "write_text", write),
        ):
            self.assertEqual(flow.analyze_commit_and_plan_step(), "HasNoCommit")
            planner.assert_called_once()
            self.assertTrue(flow.state.interface_scan_failed)

    def test_original_detect_and_plan_failures_still_propagate(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        flow = Main2MainFlow(vllm_path=str(self.up), vllm_ascend_path=str(self.down))
        with (
            patch("main2main_flow.flow.WORKSPACE_DIR", workspace),
            patch("main2main_flow.flow.detect", side_effect=ValueError("original detect failed")),
            patch("main2main_flow.flow.scan_for_flow") as scanner,
            patch("main2main_flow.flow.run_plan") as planner,
        ):
            with self.assertRaisesRegex(ValueError, "original detect failed"):
                flow.analyze_commit_and_plan_step()
            scanner.assert_not_called()
            planner.assert_not_called()
        with (
            patch("main2main_flow.flow.WORKSPACE_DIR", workspace),
            patch("main2main_flow.flow.scan_for_flow", side_effect=ValueError("scan failed")),
            patch("main2main_flow.flow.run_plan", side_effect=ValueError("original plan failed")) as planner,
        ):
            with self.assertRaisesRegex(ValueError, "original plan failed"):
                flow.analyze_commit_and_plan_step()
            planner.assert_called_once()


if __name__ == "__main__":
    unittest.main()
