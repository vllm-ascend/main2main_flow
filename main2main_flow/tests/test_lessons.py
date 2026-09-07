"""submit_pre_ci_lesson (recovery) and submit_pre_ci_exhausted_lesson
(budget out, converging) — pre_ci knowledge enters the lesson KB."""
from __future__ import annotations

from main2main_flow.scripts.utils import lessons
from main2main_flow.scripts.utils.lessons import (
    submit_pre_ci_exhausted_lesson, submit_pre_ci_lesson)


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


def test_submit_pre_ci_exhausted_lesson_records_trajectory(monkeypatch):
    captured = {}

    def _fake_submit(report_dir, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr(lessons, "_submit_via_mcp", _fake_submit)
    check_result = {
        "all_passed": False,
        "checks": [
            {"name": "format", "passed": True, "violations": []},
            {"name": "mypy", "passed": True, "violations": []},
            {"name": "ut", "passed": False, "violations": [
                "FAILED tests/ut/worker/test_model_runner_v1.py:12 - "
                "TypeError: KVCacheTensor.__init__() got an unexpected "
                "keyword argument 'shared_by'",
                "FAILED tests/ut/worker/test_attn_utils_v2.py:30 - "
                "TypeError: init_kv_cache() got an unexpected keyword "
                "argument 'attn_groups'"]},
        ],
    }
    submit_pre_ci_exhausted_lesson("/tmp/vllm-report", "step-1",
                                   check_result, [34, 30, 5])
    assert "pre_ci exhausted" in captured["title"]
    assert "34→30→5" in captured["symptom"]
    assert "test_model_runner_v1.py" in captured["symptom"]
    assert "converging" in captured["title"]
    assert "pre-ci-exhausted" in captured["tags"]
    assert any("do NOT re-analyze" in g for g in captured["fix_guidance"])
    assert any("log_path" in g for g in captured["fix_guidance"])
    assert any("120 chars" in g for g in captured["fix_guidance"])
    assert any("shared_by" in k for k in captured["keywords"])

    # empty trajectory renders as n/a; no report path / no failing checks
    # → nothing submitted
    submit_pre_ci_exhausted_lesson("/tmp/vllm-report", "step-1",
                                   check_result, [])
    assert "n/a" in captured["symptom"]
    captured.clear()
    submit_pre_ci_exhausted_lesson("", "step-1", check_result, [1])
    assert not captured
    submit_pre_ci_exhausted_lesson("/tmp/vllm-report", "step-1",
                                   {"all_passed": False, "checks": []}, [1])
    assert not captured
