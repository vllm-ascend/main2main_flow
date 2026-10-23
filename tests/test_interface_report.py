"""CPU-only contract tests; never start opencode, adaptation or device tests."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

# The import probes for the CLI. No CLI is executed by these tests.
with patch("shutil.which", return_value="opencode"):
    from main2main_flow.flow import Main2MainFlow
from main2main_flow.interface_report.handoff import load_report


class InterfaceReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report = self.root / "qa-review.md"
        self.report.write_text("# Evidence\n## abcdef0123456789 — 候选风险\n", encoding="utf-8")
        report = load_report(str(self.report))
        self.assessment = dict(report_sha256=report.sha256, step_id="fixture", start_commit="", end_commit="",
                               items=[dict(root_cause_id=report.roots[0], decision="no_change",
                                           reason="keyword callers", evidence=["vllm/api.py:10", "vllm_ascend/op.py:20"])])
        (self.root / "step_summary.md").write_text(
            "- fixture: No-op\n```interface-assessment\n" + json.dumps(self.assessment) + "\n```\n", encoding="utf-8")
        self.args = dict(ascend_path=str(self.root), vllm_path=str(self.root),
                         step_id="fixture", step_dir=str(self.root), release_tag="0.1.0")
        self.flow = Main2MainFlow()
        self.env = patch.dict(os.environ, {"MAIN2MAIN_INTERFACE_REPORT": str(self.report)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.diff = patch("main2main_flow.flow.subprocess.run",
                          return_value=subprocess.CompletedProcess([], 0, stdout="diff --git a/x b/x"))
        self.diff.start()
        self.addCleanup(self.diff.stop)

    def review(self, prompt, **kwargs):
        self.prompt = prompt
        (self.root / "review.json").write_text(json.dumps({
            "verdict": "pass", "issues": [], "interface_report_sha256": self.assessment["report_sha256"],
            "interface_step_id": "fixture", "interface_report_usage": [{
                "root_cause_id": "abcdef0123456789", "judgment": "agree",
                "assessment": "Checked keyword caller", "evidence": ["vllm_ascend/op.py:20"]}]}), encoding="utf-8")
        return "review saved", "qa-session"

    def test_report_is_optional_and_scoped(self):
        with patch("main2main_flow.flow.run_opencode_review", side_effect=self.review) as model:
            self.assertEqual(self.flow._run_adapter_qa(**self.args), ([], "qa-session"))
            model.assert_called_once()
        self.assertIn(str(self.report.resolve()), self.prompt)
        self.assertIn("PRE-adaptation", self.prompt)
        self.assertIn("future steps", self.prompt)
        self.assertIn("untrusted evidence", self.prompt)
        self.assertIn("interface_report_usage", self.prompt)
        os.environ.pop("MAIN2MAIN_INTERFACE_REPORT")
        with patch("main2main_flow.flow.run_opencode_review", side_effect=self.review):
            self.assertEqual(self.flow._run_adapter_qa(**self.args), ([], "qa-session"))
        self.assertNotIn("Interface detection reference", self.prompt)

    def test_invalid_reports_warn_and_preserve_original_diff_review(self):
        for contents in (b"", b" " * 1_000_001, b"\xff"):
            with self.subTest(contents=contents[:1]):
                self.report.write_bytes(contents)
                self.flow.state.interface_scan_failed = False
                with (
                    patch("main2main_flow.flow.run_opencode_review", side_effect=self.review) as model,
                    patch("main2main_flow.flow.warn_handoff") as warning,
                ):
                    self.assertEqual(self.flow._run_adapter_qa(**self.args), ([], "qa-session"))
                    model.assert_called_once()
                    warning.assert_called_once()
                self.assertNotIn(str(self.report), self.prompt)
        self.report.unlink()
        self.flow.state.interface_scan_failed = False
        with patch("main2main_flow.flow.run_opencode_review", side_effect=self.review) as model:
            self.assertEqual(self.flow._run_adapter_qa(**self.args), ([], "qa-session"))
            model.assert_called_once()

    def test_empty_diff_skips_review(self):
        os.environ.pop("MAIN2MAIN_INTERFACE_REPORT")
        with patch("main2main_flow.flow.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, stdout="")):
            with patch("main2main_flow.flow.run_opencode_review") as model:
                self.assertEqual(self.flow._run_adapter_qa(**self.args), ([], ""))
                model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
