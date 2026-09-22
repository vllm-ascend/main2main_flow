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


class InterfaceReportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.report = self.root / "qa-review.md"
        self.report.write_text("# Evidence {root}\n候选风险", encoding="utf-8")
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
        (self.root / "review.json").write_text(json.dumps({"verdict": "pass", "issues": []}), encoding="utf-8")
        return "review saved", "qa-session"

    def test_report_is_optional_and_scoped(self):
        with patch("main2main_flow.flow.run_opencode_review", side_effect=self.review) as model:
            self.assertEqual(self.flow._run_adapter_qa(**self.args), ([], "qa-session"))
            model.assert_called_once()
        self.assertIn(str(self.report.resolve()), self.prompt)
        self.assertIn("PRE-adaptation", self.prompt)
        self.assertIn("defer future-step", self.prompt)
        self.assertIn("untrusted evidence", self.prompt)
        self.assertIn("interface_report_usage", self.prompt)
        os.environ.pop("MAIN2MAIN_INTERFACE_REPORT")
        with patch("main2main_flow.flow.run_opencode_review", side_effect=self.review):
            self.assertEqual(self.flow._run_adapter_qa(**self.args), ([], "qa-session"))
        self.assertNotIn("Interface detection reference", self.prompt)

    def test_invalid_reports_stop_before_model(self):
        for contents in (b"", b" " * 1_000_001, b"\xff"):
            with self.subTest(contents=contents[:1]):
                self.report.write_bytes(contents)
                with patch("main2main_flow.flow.run_opencode_review") as model:
                    issues, _ = self.flow._run_adapter_qa(**self.args)
                    self.assertTrue(issues)
                    model.assert_not_called()
        self.report.unlink()
        with patch("main2main_flow.flow.run_opencode_review") as model:
            self.assertTrue(self.flow._run_adapter_qa(**self.args)[0])
            model.assert_not_called()

    def test_empty_diff_skips_review(self):
        with patch("main2main_flow.flow.subprocess.run",
                   return_value=subprocess.CompletedProcess([], 0, stdout="")):
            with patch("main2main_flow.flow.run_opencode_review") as model:
                self.assertEqual(self.flow._run_adapter_qa(**self.args), ([], ""))
                model.assert_not_called()


if __name__ == "__main__":
    unittest.main()
