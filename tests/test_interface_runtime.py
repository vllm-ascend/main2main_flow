# SPDX-License-Identifier: Apache-2.0
"""CPU-only composition tests for the production detection/QA call path."""

import json
import os
import subprocess
import unittest
from unittest.mock import patch

from main2main_flow.interface_report import runtime
from main2main_flow.interface_report.run import git
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
        flow.state.vllm_path, flow.state.vllm_ascend_path = str(self.up), str(self.down)
        flow.state.target_commit = self.new
        workspace = self.root / "workspace"
        workspace.mkdir()
        step = {"id": "step-1", "upstream_patch": "", "changed_files": "vllm/api.py"}
        calls = []

        def scan(*args):
            calls.append("scan")
            return runtime.scan_for_flow(*args, cache_dir=self.cache)

        def plan(*args, **kwargs):
            calls.append("plan")
            return {"steps": [step], "total_commits": 1}

        with (
            patch("main2main_flow.flow.WORKSPACE_DIR", workspace),
            patch("main2main_flow.flow.scan_for_flow", side_effect=scan) as scanner,
            patch("main2main_flow.flow.run_plan", side_effect=plan),
            patch("main2main_flow.flow.compute_base_ut_baseline"),
            patch("main2main_flow.flow.run_git"),
        ):
            self.assertEqual(flow.analyze_commit_and_plan_step(), "HasCommit")
            self.assertEqual(calls, ["scan", "plan"])
            scanner.assert_called_once()

        prompts = []
        step_dir = workspace / "steps/step-1"

        def review(prompt, **kwargs):
            prompts.append(prompt)
            (step_dir / "review.json").write_text('{"verdict":"pass","issues":[]}', encoding="utf-8")
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

    def test_no_change_skips_scan_and_scan_failure_stops_before_plan(self):
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
        with (
            patch("main2main_flow.flow.WORKSPACE_DIR", workspace),
            patch("main2main_flow.flow.scan_for_flow", side_effect=ValueError("scan failed")),
            patch("main2main_flow.flow.run_plan") as planner,
            patch.object(flow, "process_steps") as adapter,
        ):
            with self.assertRaisesRegex(ValueError, "scan failed"):
                flow.analyze_commit_and_plan_step()
            planner.assert_not_called()
            adapter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
