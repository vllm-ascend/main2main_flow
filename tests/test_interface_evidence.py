# SPDX-License-Identifier: Apache-2.0
"""Verify actionable source evidence and honest unknown state descriptions."""
import copy
import re
import unittest

from main2main_flow.interface_report.handoff import load_report
from main2main_flow.interface_report.qa_markdown import render_qa, source_excerpt
import test_interface_scan as fixtures


class EvidenceTests(unittest.TestCase):
    write = staticmethod(fixtures.InterfaceTests.write)
    commit = staticmethod(fixtures.InterfaceTests.commit)

    def setUp(self):
        fixtures.InterfaceTests.setUp(self)
        self.addCleanup(self.temp.cleanup)

    def test_complete_signature_and_scoped_returns_exclude_neighbor_functions(self):
        self.write(self.up, "vllm/proof.py", """def unrelated():
    return 'UNRELATED'

def forward(
    self,
    hidden_states,
    optional=None,
    *,
    flag=False,
):
    def nested():
        return 'NESTED'
    output = project(hidden_states)
    if flag:
        return output
    return None
""")
        sha = self.commit(self.up)
        text = source_excerpt(self.up, sha, {"file": "vllm/proof.py", "line": 4, "name": "forward"}, "vllm-project/vllm")
        self.assertIn("optional=None", text)
        self.assertIn("flag=False", text)
        self.assertIn("output = project", text)
        self.assertIn("if flag:", text)
        self.assertIn("return output", text)
        self.assertIn("return None", text)
        self.assertNotIn("UNRELATED", text)
        self.assertNotIn("NESTED", text)
        self.assertNotIn("无显式 return", text)

    def test_implicit_none_and_unavailable_evidence_are_explicit(self):
        self.write(self.up, "vllm/proof.py", "def forward(self, hidden_states, output):\n    output[:] = project(hidden_states)\n")
        sha = self.commit(self.up)
        endpoint = {"file": "vllm/proof.py", "line": 1, "name": "forward"}
        text = source_excerpt(self.up, sha, endpoint, "")
        self.assertIn("output[:] =", text)
        self.assertIn("若正常执行到末尾则返回 None", text)
        self.assertIn("扫描基线提交", text)
        self.assertNotIn("临时重放", text)
        self.assertIn("行号超出文件范围", source_excerpt(self.up, sha, {**endpoint, "line": 99}, ""))
        self.write(self.up, "vllm/proof.py", "def future_syntax[ T ]():\n    pass\n")
        sha = self.commit(self.up)
        # Current supported test runtimes may differ in PEP-695 support.
        text = source_excerpt(self.up, sha, {**endpoint, "line": None}, "")
        self.assertIn("位置未知，证据不足", text)

    def test_state_report_shows_real_consumers_and_constructor_read_proofs(self):
        self.write(self.up, "vllm/base.py", """class Base:
    def __init__(self):
        self._metadata = None
    def get_metadata(self):
        assert self._metadata is not None
        return self._metadata
    def has_metadata(self):
        return self._metadata is not None
""")
        new = self.commit(self.up)
        self.write(self.down, "vllm_ascend/consumer.py", """from vllm.base import Base
class Consumer(Base):
    def __init__(self):
        super().__init__()
""")
        sha = self.commit(self.down)
        evidence = dict(downstream_class="vllm_ascend.consumer.Consumer", downstream_file="vllm_ascend/consumer.py",
                        upstream_root="vllm.base.Base", inherited_member="vllm.base.Base.get_metadata",
                        required_attribute="_metadata", read_line=5, constructor_owner="Base",
                        constructor_file="vllm/base.py", constructor_line=3, initialization_status="established")
        finding = dict(id="1234567890abcdef", root_cause_id="abcdef0123456789", classification="analysis_unresolved",
                       relation="inherited_state", contract_kind="required_instance_attribute",
                       change="DO_NOT_USE_AS_BOTH_CONTRACTS", gates={"contract_changed": True},
                       upstream={"old": dict(file="vllm/base.py", owner="Base", name="get_metadata", line=None),
                                 "new": dict(file="vllm/base.py", owner="Base", name="get_metadata", line=4)},
                       downstream=dict(file="vllm/base.py", owner="Base", name="_metadata", line=3),
                       compatibility={"old": dict(exists=None, compatible=None, reason="unknown inherited binding"),
                                      "new": dict(exists=True, compatible=True, reason="constructor establishes attribute")},
                       evidence=[evidence, {**evidence, "inherited_member": "vllm.base.Base.has_metadata", "read_line": 8}],
                       details=dict(required_attribute="_metadata", initialization_status="established",
                                    inherited_members=["vllm.base.Base.get_metadata", "vllm.base.Base.has_metadata"]))
        report = dict(findings=[finding], metadata={"range_analyzer_version": "fixture"})
        original = copy.deepcopy(report)
        text = render_qa(report, dict(vllm_old_sha=self.old, vllm_new_sha=new, vllm_ascend_sha=sha, engine_sha="fixture"),
                         dict(vllm_root=self.up, ascend_root=self.down), {"source_mode": "incremental_rebased"})
        self.assertEqual(report, original)
        self.assertIn("未知：继承绑定或状态行为尚未证实", text)
        self.assertIn("已确认：构造函数建立字段 _metadata", text)
        self.assertNotIn("DO_NOT_USE_AS_BOTH_CONTRACTS", text)
        self.assertIn("字段存在不等于调用时值或生命周期有效", text)
        self.assertIn("受影响 Ascend 类", text)
        self.assertIn("vllm_ascend/consumer.py:2", text)
        self.assertIn("上游所需字段定义", text)
        self.assertIn("super().__init__()", text)
        self.assertEqual(text.count("assert self._metadata is not None"), 1)
        self.assertIn("Base.has_metadata", text)
        self.assertIn("本文件不回填这些结论", text)
        self.assertNotIn("QA 尚未调用", text)
        self.assertNotIn("只读审阅，不修改代码", text)
        links = set(re.findall(r"\]\(#(evidence-s\d+)\)", text))
        anchors = re.findall(r'<a id="(evidence-s\d+)"></a>', text)
        self.assertEqual(links, set(anchors))
        self.assertEqual(len(anchors), len(set(anchors)))
        path = self.root / "qa-review.md"
        path.write_text(text, encoding="utf-8")
        self.assertEqual(load_report(str(path)).roots, (finding["root_cause_id"],))
        self.assertIn(finding["id"], text)
