# SPDX-License-Identifier: Apache-2.0
"""Exercise rejected candidates, malformed handoffs and the real adapter loop."""

import copy
import json
import os
from contextlib import ExitStack
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

with patch("shutil.which", return_value="opencode"):
    from main2main_flow.flow import Main2MainFlow
    from main2main_flow.scripts.agent.opencode_adapter import AdaptResult, _build_prompt

from main2main_flow.interface_report.handoff import adapter_context, load_assessment, load_report, validate_review


class HandoffTests(unittest.TestCase):
    def test_merged_report_retains_all_roots_and_rejects_duplicates(self):
        self.path.write_text("## 接口：`API.forward`\n#### abcdef0123456789 — 入参变化\n"
                             "### fedcba9876543210 — 返回值变化\n", encoding="utf-8")
        report = load_report(str(self.path))
        self.assertEqual(report.roots, ("abcdef0123456789", "fedcba9876543210"))
        assessment = {**self.assessment, "report_sha256": report.sha256}
        self.write_assessment(assessment)
        with self.assertRaisesRegex(ValueError, "missing root"):
            load_assessment(report, self.step_dir, "s1", "a" * 40, "b" * 40)
        self.path.write_text(self.path.read_text(encoding="utf-8") +
                             "## abcdef0123456789 — legacy duplicate\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "duplicate"):
            load_report(str(self.path))

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.step_dir = self.root / "steps/s1"
        self.step_dir.mkdir(parents=True)
        self.path = self.root / "qa-review.md"
        self.path.write_text("# Evidence\n## abcdef0123456789 — candidate\n", encoding="utf-8")
        self.report = load_report(str(self.path))
        self.step = dict(id="s1", start_commit="a" * 40, end_commit="b" * 40, upstream_patch="diff")
        self.assessment = dict(
            report_sha256=self.report.sha256, step_id="s1", start_commit="a" * 40, end_commit="b" * 40,
            items=[dict(root_cause_id=self.report.roots[0], decision="no_change", reason="keyword callers",
                        evidence=["vllm/api.py:1", "vllm_ascend/caller.py:3"])],
        )
        self.write_assessment()
        self.flow = Main2MainFlow()
        self.flow.state.steps = [self.step]
        self.flow.state.total_steps = 1
        self.flow.state.interface_report = str(self.path)
        self.flow.state.vllm_path = self.flow.state.vllm_ascend_path = str(self.root)
        self.args = dict(ascend_path=str(self.root), vllm_path=str(self.root), step_id="s1",
                         step_dir=str(self.step_dir), release_tag="0.1.0")
        self.review = dict(
            verdict="pass", issues=[], interface_report_sha256=self.report.sha256, interface_step_id="s1",
            interface_report_usage=[dict(root_cause_id=self.report.roots[0], judgment="agree",
                                        assessment="independently checked actual keyword caller",
                                        evidence=["vllm_ascend/caller.py:3"])],
        )

    def write_assessment(self, data=None):
        (self.step_dir / "step_summary.md").write_text(
            "- s1: No-op — verified keyword callers\n```interface-assessment\n"
            + json.dumps(self.assessment if data is None else data) + "\n```\n", encoding="utf-8")

    def model(self, prompt, **kwargs):
        self.prompt = prompt
        (self.step_dir / "review.json").write_text(json.dumps(self.review), encoding="utf-8")
        return "saved", "independent-qa"

    def test_rejected_root_with_empty_diff_still_reaches_independent_qa(self):
        with (
            patch("main2main_flow.flow.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="")),
            patch("main2main_flow.flow.run_opencode_review", side_effect=self.model) as model,
        ):
            self.assertEqual(self.flow._run_adapter_qa(**self.args), ([], "independent-qa"))
        model.assert_called_once()
        self.assertIn(str(self.step_dir / "step_summary.md"), self.prompt)
        self.assertIn("keyword callers", self.prompt)
        self.assertIn("not proof", self.prompt)
        self.assertIn("no changed file", self.prompt)

    def test_qa_can_disagree_with_adapter_and_return_actionable_fix(self):
        self.review["verdict"] = "fail"
        self.review["interface_report_usage"][0]["judgment"] = "disagree"
        self.review["issues"] = [dict(file="vllm_ascend/caller.py", line=3, issue="positional caller remains")]
        with (
            patch("main2main_flow.flow.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="")),
            patch("main2main_flow.flow.run_opencode_review", side_effect=self.model),
        ):
            issues, _ = self.flow._run_adapter_qa(**self.args)
        self.assertIn("positional caller remains", issues[0])

    def test_missing_stale_or_incomplete_adapter_decisions_stop_before_qa(self):
        cases = []
        for field in ("report_sha256", "step_id", "start_commit", "end_commit"):
            data = copy.deepcopy(self.assessment)
            data[field] = "stale"
            cases.append(data)
        for field, value in (("root_cause_id", "unknown"), ("decision", "ignore"),
                             ("reason", ""), ("evidence", []), ("decision", [])):
            data = copy.deepcopy(self.assessment)
            data["items"][0][field] = value
            cases.append(data)
        cases += [dict(self.assessment, items=[]), dict(self.assessment, items=self.assessment["items"] * 2)]
        for data in cases:
            with self.subTest(data=data):
                self.write_assessment(data)
                with self.assertRaises(ValueError):
                    load_assessment(self.report, self.step_dir, "s1", "a" * 40, "b" * 40)
        (self.step_dir / "step_summary.md").unlink()
        with (
            patch("main2main_flow.flow.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="")),
            patch("main2main_flow.flow.run_opencode_review") as model,
        ):
            self.assertTrue(self.flow._run_adapter_qa(**self.args)[0][0].startswith("interface-handoff:"))
            model.assert_not_called()

    def test_qa_missing_roots_stale_identity_and_false_pass_are_rejected(self):
        for field, value in (("interface_report_sha256", "old"), ("interface_step_id", "old"),
                             ("interface_report_usage", [])):
            data = copy.deepcopy(self.review)
            data[field] = value
            with self.assertRaises(ValueError):
                validate_review(data, self.report, "s1")
        self.review["interface_report_usage"][0]["judgment"] = "disagree"
        with self.assertRaisesRegex(ValueError, "disagreement"):
            validate_review(self.review, self.report, "s1")

    def test_report_context_survives_adapter_and_fix_prompt_building(self):
        for role in ("adapter", "adapter-fix"):
            inputs = dict(self.args, role=role, previous_step_summary_path="", is_last_step=False,
                          patch_path="upstream.patch", changed_files_path="changed.txt", error_logs="[]",
                          code_structure_guide_file="guide.md", mode=role,
                          **self.flow._interface_adapter_inputs(self.step))
            prompt, _ = _build_prompt(inputs)
            self.assertIn(str(self.path), prompt)
            self.assertIn(self.report.sha256, prompt)
            self.assertIn("no_change", prompt)
            self.assertIn("actual downstream", prompt)
        self.path.unlink()
        with self.assertRaises(ValueError):
            adapter_context(str(self.path), "s1", "a", "b")

    def test_zero_roots_empty_diff_preserves_no_model_fast_path(self):
        self.path.write_text("# Report\nNo expanded candidates\n", encoding="utf-8")
        with (
            patch("main2main_flow.flow.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="")),
            patch("main2main_flow.flow.run_opencode_review") as model,
        ):
            self.assertEqual(self.flow._run_adapter_qa(**self.args), ([], ""))
            model.assert_not_called()

    def test_bound_report_cannot_be_replaced_after_scan(self):
        self.flow.state.interface_report_sha256 = self.report.sha256
        self.path.write_text("# replaced report", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "changed after scanning"):
            self.flow._interface_adapter_inputs(self.step)

    def test_stale_review_is_removed_and_no_output_does_not_pass(self):
        path = self.step_dir / "review.json"
        path.write_text(json.dumps(self.review), encoding="utf-8")
        with (
            patch("main2main_flow.flow.subprocess.run", return_value=subprocess.CompletedProcess([], 0, stdout="")),
            patch("main2main_flow.flow.run_opencode_review", return_value=("", "failed-qa")),
        ):
            issues, _ = self.flow._run_adapter_qa(**self.args)
        self.assertTrue(issues[0].startswith("interface-handoff:"))
        self.assertFalse(path.exists())

    def test_deferred_and_insufficient_evidence_are_explicit_supported_outcomes(self):
        for decision in ("deferred", "insufficient_evidence"):
            self.assessment["items"][0]["decision"] = decision
            self.write_assessment()
            self.assertIsNotNone(load_assessment(self.report, self.step_dir, "s1", "a" * 40, "b" * 40))
            self.review["interface_report_usage"][0]["judgment"] = decision
            validate_review(self.review, self.report, "s1")

    def test_production_loop_passes_report_to_adapter_and_fix_and_rechecks_noop(self):
        # Even an empty per-step patch cannot bypass pending candidate review.
        self.step["upstream_patch"] = ""
        calls = []

        def adapter(inputs, **kwargs):
            calls.append(inputs)
            if len(calls) == 1:
                (self.step_dir / "step_summary.md").unlink()
            else:
                self.write_assessment()
            return AdaptResult(is_noop=True, step_summary="noop", session_id="adapter-session")

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {"SKIP_AI_ANALYSIS": "false"}))
            for name, value in (
                ("WORKSPACE_DIR", self.root), ("run_git", lambda *a, **k: ""),
                ("run_update", lambda **k: {"files_updated": []}),
                ("_build_upstream_fix_diff", lambda *a: ""),
                ("_revert_e2e_test_edits", lambda *a: []),
                ("run_check", lambda *a, **k: {"all_passed": True, "checks": []}),
                ("record_pre_ci_attempt", lambda *a: None),
            ):
                stack.enter_context(patch("main2main_flow.flow." + name, value))
            stack.enter_context(patch("main2main_flow.flow.subprocess.run",
                                      return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")))
            stack.enter_context(patch("main2main_flow.flow.run_opencode_adapter", side_effect=adapter))
            qa = stack.enter_context(patch("main2main_flow.flow.run_opencode_review", side_effect=self.model))
            self.assertTrue(self.flow._ai_analysis())
        self.assertEqual([c["role"] for c in calls], ["adapter", "adapter-fix"])
        self.assertTrue(all(c["interface_report"] == str(self.path) for c in calls))
        self.assertTrue(all(c["end_commit"] == "b" * 40 for c in calls))
        qa.assert_called_once()
        self.assertTrue(self.flow.state.last_step_is_noop)

    def test_invalid_handoffs_cannot_use_semantic_qa_escape_after_two_attempts(self):
        for malformed_adapter in (True, False):
            with self.subTest(malformed_adapter=malformed_adapter), ExitStack() as stack:
                self.flow.state.session_id = ""
                self.flow.state.test_errors = []

                def adapter(inputs, **kwargs):
                    self.write_assessment()
                    if malformed_adapter:
                        (self.step_dir / "step_summary.md").unlink()
                    return AdaptResult(is_noop=True, step_summary="noop")

                def model(prompt, **kwargs):
                    (self.step_dir / "review.json").write_text('{"verdict":"pass","issues":[]}', encoding="utf-8")
                    return "saved", "qa"

                stack.enter_context(patch.dict(os.environ, {"SKIP_AI_ANALYSIS": "false"}))
                for name, value in (
                    ("WORKSPACE_DIR", self.root), ("run_git", lambda *a, **k: ""),
                    ("run_update", lambda **k: {"files_updated": []}),
                    ("_build_upstream_fix_diff", lambda *a: ""), ("_revert_e2e_test_edits", lambda *a: []),
                    ("record_pre_ci_attempt", lambda *a: None),
                    ("submit_pre_ci_exhausted_lesson", lambda *a, **k: None),
                ):
                    stack.enter_context(patch("main2main_flow.flow." + name, value))
                stack.enter_context(patch("main2main_flow.flow.subprocess.run",
                                          return_value=subprocess.CompletedProcess([], 0, stdout="", stderr="")))
                adapters = stack.enter_context(patch("main2main_flow.flow.run_opencode_adapter", side_effect=adapter))
                check = stack.enter_context(patch("main2main_flow.flow.run_check",
                                                  return_value={"all_passed": True, "checks": []}))
                qa = stack.enter_context(patch("main2main_flow.flow.run_opencode_review", side_effect=model))
                self.assertFalse(self.flow._ai_analysis())
                self.assertEqual(adapters.call_count, 5)
                self.assertEqual(qa.call_count, 0 if malformed_adapter else 5)
                self.assertEqual(check.call_count, 0 if malformed_adapter else 5)


if __name__ == "__main__":
    unittest.main()
