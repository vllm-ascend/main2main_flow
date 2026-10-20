# SPDX-License-Identifier: Apache-2.0
"""Patch definitions and installations must retain their own file/line pairs."""

import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

from main2main_flow.interface_contracts.generator import Relation, RelationEvidence
from main2main_flow.interface_contracts.range_analysis import analyze_range, _relation_downstream_endpoint
from main2main_flow.interface_report.qa_markdown import render_qa
import test_interface_scan as fixtures


class PatchLocationTests(unittest.TestCase):
    write = staticmethod(fixtures.InterfaceTests.write)
    commit = staticmethod(fixtures.InterfaceTests.commit)

    def setUp(self):
        fixtures.InterfaceTests.setUp(self)
        self.addCleanup(self.temp.cleanup)

    def test_scan_and_markdown_locate_cross_file_and_local_patches(self):
        self.write(self.up, "vllm/api.py", "class API:\n    def forward(self, value, output):\n        pass\n    def other(self, value, output):\n        pass\n")
        old = self.commit(self.up)
        self.write(self.up, "vllm/api.py", "class API:\n    def forward(self, value) -> int:\n        return 1\n    def other(self, value) -> int:\n        return 1\n")
        new = self.commit(self.up)
        self.write(self.down, "vllm_ascend/impl.py", "# implementation\nclass Replacement:\n    def forward(self, value, output):\n        pass\n")
        self.write(self.down, "vllm_ascend/patch.py", "from vllm.api import API\nfrom vllm_ascend.impl import Replacement\n\ndef other(self, value, output):\n    pass\n\n# installation\nAPI.forward = Replacement.forward\nAPI.other = other\n")
        self.write(self.down, "vllm_ascend/override.py", "from vllm.api import API\nclass Override(API):\n    def forward(self, value, output):\n        pass\n")
        sha = self.commit(self.down)
        report = analyze_range(vllm_root=self.up, ascend_root=self.down, old=old, new=new,
                               expect_ascend_sha=sha, analysis_workers=1, cache_enabled=False)
        patches = [f for f in report["findings"] if f["relation"] == "monkey_patch"]
        self.assertEqual({f["downstream"]["file"] for f in patches},
                         {"vllm_ascend/impl.py", "vllm_ascend/patch.py"})
        for finding in patches:
            endpoint = finding["downstream"]
            definition, installation = (3, 8) if endpoint["file"].endswith("impl.py") else (4, 9)
            self.assertEqual(endpoint["line"], definition)
            self.assertEqual(finding["classification"], "introduced_break")
            self.assertEqual(finding["evidence"][0]["file"], "vllm_ascend/patch.py")
            self.assertEqual(finding["evidence"][0]["line"], installation)
        overrides = [f for f in report["findings"] if f["relation"] == "override"]
        self.assertTrue(overrides)
        self.assertTrue(all(f["downstream"]["line"] == 3 for f in overrides))
        text = render_qa(report, dict(vllm_old_sha=old, vllm_new_sha=new, vllm_ascend_sha=sha,
                                     engine_sha="fixture"),
                         dict(vllm_root=self.up, ascend_root=self.down), {})
        self.assertIn("方法定义位置", text)
        self.assertIn("补丁安装位置", text)
        self.assertIn(f"/blob/{sha}/vllm_ascend/impl.py#L3", text)
        self.assertIn(f"/blob/{sha}/vllm_ascend/patch.py#L8", text)
        self.assertIn(f"/blob/{sha}/vllm_ascend/patch.py#L4", text)
        self.assertIn(f"/blob/{sha}/vllm_ascend/patch.py#L9", text)
        self.assertNotIn(f"/blob/{sha}/vllm_ascend/impl.py#L8", text)
        self.assertIn("API.forward = Replacement.forward", text)

    def test_unresolved_definition_never_borrows_installation_line(self):
        relation = Relation(relation="monkey_patch", upstream_file="vllm/api.py", upstream_owner="API",
                            upstream_name="forward", upstream_signature=None,
                            downstream_file="vllm_ascend/impl.py", downstream_owner="Replacement",
                            downstream_name="forward", downstream_signature=None,
                            evidence_file="vllm_ascend/patch.py", evidence_line=207)
        index = Mock()
        index.find_callable.return_value = None
        engine = SimpleNamespace(downstream=index)
        evidence = RelationEvidence(file=relation.evidence_file, line=207, definition_line=67)
        for records, expected in [((), None), ((evidence,), 67),
                                  ((evidence, replace(evidence, definition_line=70)), None)]:
            with self.subTest(records=records):
                endpoint = _relation_downstream_endpoint(replace(relation, evidence=records), engine)
                self.assertEqual(endpoint.file, "vllm_ascend/impl.py")
                self.assertEqual(endpoint.line, expected)
