"""submit_pre_ci_lesson — pre_ci recoveries enter the lesson KB."""
from __future__ import annotations

from main2main_flow.scripts.utils import lessons
from main2main_flow.scripts.utils.lessons import submit_pre_ci_lesson


def test_submit_pre_ci_lesson_extracts_keywords_and_skips_empty(
        monkeypatch):
    captured = {}

    def _fake_submit(report_dir, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(lessons, "_submit_via_mcp", _fake_submit)
    check_result = {
        "all_passed": False,
        "checks": [
            {"name": "format", "passed": True, "violations": []},
            {"name": "mypy", "passed": False, "violations": [
                "vllm_ascend/kv_cache.py:120:5: error: Item \"None\" has "
                "no attribute \"shared_by\" [union-attr]"]},
            {"name": "ut", "passed": False, "violations": [
                "FAILED tests/ut/python/test_kv.py::test_mla - "
                "AttributeError: 'MLAAttentionSpec' object has no attribute "
                "'compress_ratio'"]},
        ],
    }
    submit_pre_ci_lesson("/tmp/vllm-report", "step-1", check_result)
    assert captured["title"].startswith("step-1: pre_ci fix needed")
    assert "mypy" in captured["title"] and "ut" in captured["title"]
    assert "compress_ratio" in captured["keywords"][0] or any(
        "compress_ratio" in k for k in captured["keywords"])
    assert "pre-ci-fix" in captured["tags"]
    assert any("upstream-contract-drift.md" in g for g in
               captured["fix_guidance"])
    assert any("log_path" in g for g in captured["fix_guidance"])

    # empty failing checks → no submission
    captured.clear()
    submit_pre_ci_lesson("/tmp/vllm-report", "step-1",
                         {"all_passed": True, "checks": [
                             {"name": "ut", "passed": True,
                              "violations": []}]})
    assert not captured


def test_submit_pre_ci_lesson_noop_without_report_path(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("must not submit without vllm_report_path")

    monkeypatch.setattr(lessons, "_submit_via_mcp", _boom)
    submit_pre_ci_lesson("", "step-1", {"all_passed": False, "checks": [
        {"name": "ut", "passed": False, "violations": ["FAILED x"]}]}
    )
    submit_pre_ci_lesson("/tmp/vllm-report", "step-1", {})
