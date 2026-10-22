# SPDX-License-Identifier: Apache-2.0
"""Compare historical and current inheritance without executing scanned code."""

import unittest

from main2main_flow.interface_contracts.generator import InterfaceBoundaryGenerator
from main2main_flow.interface_contracts.range_analysis import GitSnapshot, _snapshot_inherited_reads, analyze_range
from main2main_flow.interface_report.handoff import load_report
from main2main_flow.interface_report.qa_markdown import render_qa
import test_interface_scan as fixtures


class InheritanceTests(unittest.TestCase):
    write = staticmethod(fixtures.InterfaceTests.write)
    commit = staticmethod(fixtures.InterfaceTests.commit)

    def setUp(self):
        fixtures.InterfaceTests.setUp(self)
        self.addCleanup(self.temp.cleanup)

    def snapshot(self, sha):
        snapshot = GitSnapshot(self.up, sha)
        self.addCleanup(snapshot.close)
        return snapshot

    def consumer(self, initializer):
        self.write(self.down, "vllm_ascend/connector.py", "from vllm.connector import Connector\n"
                   "class AscendConnector(Connector):\n    def __init__(self):\n        " + initializer + "\n")
        return self.commit(self.down)

    def analyze(self, old, new, ascend):
        return analyze_range(vllm_root=self.up, ascend_root=self.down, old=old, new=new,
                             expect_ascend_sha=ascend, analysis_workers=1, cache_enabled=False)

    def test_unchanged_multiple_inheritance_reads_are_excluded(self):
        self.write(self.up, "vllm/connector.py", """class Base:
    def __init__(self):
        self._role = 'worker'
        self._metadata = None
    @property
    def role(self):
        return self._role
    def get_metadata(self):
        assert self._metadata is not None
        return self._metadata
class SupportsHMA:
    pass
class Connector(Base, SupportsHMA):
    pass
""")
        old = self.commit(self.up)
        self.write(self.up, "vllm/unrelated.py", "changed = True\n")
        new = self.commit(self.up)
        ascend = self.consumer("super().__init__()")
        for sha in (old, new):
            for member, field in (("role", "_role"), ("get_metadata", "_metadata")):
                reads, endpoint = _snapshot_inherited_reads(self.snapshot(sha), "vllm.connector.Connector", member)
                self.assertIn(field, reads)
                self.assertEqual(endpoint.owner, "Base")
        report = self.analyze(old, new, ascend)
        self.assertFalse([f for f in report["findings"] if f["relation"] == "inherited_state"])

    def test_diamond_c3_and_reexports_match_current_index(self):
        self.write(self.up, "vllm/base.py", """class Base:
    def value(self):
        return self._base
class Left(Base):
    pass
class Right(Base):
    def value(self):
        return self._right
""")
        self.write(self.up, "vllm/export.py", "from vllm.base import Left as ExportedLeft\n")
        self.write(self.up, "vllm/connector.py", "from vllm.export import ExportedLeft\n"
                   "from vllm.base import Right\nclass Connector(ExportedLeft, Right):\n    pass\n")
        old = self.commit(self.up)
        self.write(self.up, "vllm/unrelated.py", "changed = True\n")
        new = self.commit(self.up)
        index = InterfaceBoundaryGenerator(self.up, self.down)
        mro = index._linearized_mro("vllm.connector.Connector")
        self.assertTrue(mro.complete)
        self.assertEqual(mro.owners, ("vllm.connector.Connector", "vllm.base.Left", "vllm.base.Right", "vllm.base.Base"))
        for sha in (old, new):
            snapshot = self.snapshot(sha)
            self.assertEqual(snapshot._linearized_mro("vllm.connector.Connector"), mro)
            reads, endpoint = _snapshot_inherited_reads(snapshot, "vllm.connector.Connector", "value")
            self.assertEqual(set(reads), {"_right"})
            self.assertEqual(endpoint.owner, "Right")

    def test_changed_base_order_uses_each_revisions_own_owner(self):
        prefix = """class First:
    def value(self):
        return self._first
class Second:
    def value(self):
        return self._second
"""
        self.write(self.up, "vllm/connector.py", prefix + "class Connector(First, Second):\n    pass\n")
        old = self.commit(self.up)
        self.write(self.up, "vllm/connector.py", prefix + "class Connector(Second, First):\n    pass\n")
        new = self.commit(self.up)
        for sha, owner, field in ((old, "First", "_first"), (new, "Second", "_second")):
            reads, endpoint = _snapshot_inherited_reads(self.snapshot(sha), "vllm.connector.Connector", "value")
            self.assertEqual(endpoint.owner, owner)
            self.assertEqual(set(reads), {field})

    def state_range(self, initializer, opaque_old=False):
        before = """class Base:
    def __init__(self):
        self._role = 'worker'
    def value(self):
        return 0
class Mixin:
    pass
class Connector(Base, Mixin):
    pass
"""
        if opaque_old:
            before = before.replace("return 0", "return self._role").replace("Connector(Base, Mixin)", "Connector(Base, Opaque)")
        self.write(self.up, "vllm/connector.py", before)
        old = self.commit(self.up)
        after = before.replace("return 0", "return self._role").replace("Connector(Base, Opaque)", "Connector(Base, Mixin)")
        self.write(self.up, "vllm/connector.py", after)
        new = self.commit(self.up)
        ascend = self.consumer(initializer)
        return self.analyze(old, new, ascend), old, new, ascend

    def test_new_read_with_missing_super_remains_introduced_break(self):
        report, *_ = self.state_range("pass")
        findings = [f for f in report["findings"] if f["relation"] == "inherited_state"]
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["classification"], "introduced_break")
        self.assertEqual(finding["action"], "modify")
        self.assertTrue(all(finding["gates"].values()))
        self.assertTrue(finding["compatibility"]["old"]["compatible"])
        self.assertFalse(finding["compatibility"]["new"]["compatible"])
        self.assertEqual(finding["upstream"]["old"]["owner"], "Base")

    def test_new_read_with_super_is_compatible(self):
        report, *_ = self.state_range("super().__init__()")
        findings = [f for f in report["findings"] if f["relation"] == "inherited_state"]
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["classification"], "compatibility_warning")
        self.assertTrue(findings[0]["compatibility"]["new"]["compatible"])
        self.assertNotEqual(findings[0]["action"], "modify")

    def test_opaque_old_binding_is_unknown_not_a_claimed_change(self):
        report, old, new, ascend = self.state_range("super().__init__()", opaque_old=True)
        findings = [f for f in report["findings"] if f["relation"] == "inherited_state"]
        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding["classification"], "analysis_unresolved")
        self.assertIsNone(finding["gates"]["contract_changed"])
        self.assertIsNone(finding["compatibility"]["old"]["compatible"])
        self.assertEqual(finding["action"], "review")
        self.assertNotIn("newly reads", finding["change"])
        self.assertNotIn("Initialize", finding["suggestion"])
        text = render_qa(report, dict(vllm_old_sha=old, vllm_new_sha=new, vllm_ascend_sha=ascend, engine_sha="fixture"),
                         dict(vllm_root=self.up, ascend_root=self.down), {})
        self.assertIn(finding["id"], text)
        self.assertIn("待确认，未证明新增 break", text)
        path = self.root / "qa-review.md"
        path.write_text(text, encoding="utf-8")
        self.assertIn(finding["root_cause_id"], load_report(str(path)).roots)

    def test_invalid_cyclic_and_opaque_mro_fail_closed(self):
        sources = {
            "invalid": "class X: pass\nclass Y: pass\nclass A(X, Y): pass\nclass B(Y, X): pass\nclass Connector(A, B): pass\n",
            "cycle": "class A(Connector): pass\nclass Connector(A): pass\n",
            "opaque": "class A: pass\nclass Connector(A, Unknown): pass\n",
            "decorated": "@replace_class\nclass A: pass\nclass Connector(A): pass\n",
        }
        for name, source in sources.items():
            with self.subTest(name=name):
                self.write(self.up, "vllm/connector.py", source)
                sha = self.commit(self.up)
                snapshot = self.snapshot(sha)
                self.assertFalse(snapshot._linearized_mro("vllm.connector.Connector").complete)
                reads, _ = _snapshot_inherited_reads(snapshot, "vllm.connector.Connector", "value")
                self.assertIsNone(reads)

    def test_non_callable_shadow_does_not_fall_through_to_base(self):
        self.write(self.up, "vllm/connector.py", """class Base:
    def value(self):
        return self._role
class Shadow:
    value = None
class Connector(Shadow, Base):
    pass
""")
        sha = self.commit(self.up)
        reads, endpoint = _snapshot_inherited_reads(self.snapshot(sha), "vllm.connector.Connector", "value")
        self.assertEqual(reads, {})
        self.assertEqual(endpoint.owner, "Shadow")


if __name__ == "__main__":
    unittest.main()
