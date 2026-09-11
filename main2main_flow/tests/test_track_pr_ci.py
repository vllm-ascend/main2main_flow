"""PR CI lane classification (main pin vs release tag) and the
conflicting-PR branch — a release-only failure means a pinned-release
adaptation gap, a signal "failure" alone does not carry."""
from __future__ import annotations

import json
from pathlib import Path

from main2main_flow.scripts.utils import track_pr_ci as tp


def test_lane_from_check_name_current_formats():
    # pr_test.yaml: run-selected-tests (vllm@${{ matrix.vllm_version }})
    main_name = ("run-selected-tests (vllm@8ec9a879f1bba6a1d2d3e4f506172839"
                 "4a5b6c7d) / linux-aarch64-a2 / test_basic")
    assert tp._lane_from_check_name(main_name) == "main"
    assert tp._lane_from_check_name(
        "run-selected-tests (vllm@v0.28.0) / linux-aarch64-a2 / test_basic"
    ) == "release"
    # tag without the v prefix still classifies as release
    assert tp._lane_from_check_name(
        "run-selected-tests (vllm@0.28.0) / cpu-0") == "release"


def test_lane_from_check_name_legacy_and_garbage():
    # Legacy names predate the dual-lane split and only ran the release pin
    assert tp._lane_from_check_name(
        "run-selected-tests (v0.27.1) / cpu-0 card-(part 1-1)") == "release"
    # unparseable names never misreport as main/release
    assert tp._lane_from_check_name("build-wheel") == "unknown"
    assert tp._lane_from_check_name(
        "run-selected-tests (vllm@not-a-version) / cpu-0") == "unknown"


def test_ci_class_mapping():
    assert tp._ci_class(set()) == "none"
    assert tp._ci_class({"release"}) == "release-only"
    assert tp._ci_class({"main"}) == "main"
    assert tp._ci_class({"main", "release"}) == "both"
    # unknown lanes never masquerade as a known lane
    assert tp._ci_class({"unknown"}) == "unknown"
    assert tp._ci_class({"main", "unknown"}) == "main"


def test_get_checks_tags_lane(monkeypatch):
    fixture = [
        {"name": "run-selected-tests (vllm@v0.28.0) / cpu-0",
         "state": "FAILURE", "bucket": "fail", "link": ""},
        {"name": "run-selected-tests (vllm@" + "a" * 40 + ") / cpu-0",
         "state": "SUCCESS", "bucket": "pass", "link": ""},
        {"name": "skipped-thing", "state": "SKIPPED", "bucket": "skipping",
         "link": ""},
    ]
    monkeypatch.setattr(tp, "_gh_json", lambda *a, **k: fixture)
    monkeypatch.setattr(tp, "_extract_failure_summary", lambda job_id: None)
    checks = tp._get_checks(1, skip_log_fetch=True)
    assert [c["lane"] for c in checks] == ["release", "main"]
    assert checks[0]["conclusion"] == "failure"


def _track(monkeypatch, tmp_path: Path, checks, gh_view=None):
    pr = {"number": 16296, "title": "adapt to vLLM main (8ec9a87)",
          "createdAt": "2026-09-10T00:00:00Z", "state": "open",
          "url": "https://github.com/x", "labels": [],
          "vllm_commit": "8ec9a87"}
    monkeypatch.setattr(tp, "_gh_pr_list", lambda since: [pr])
    monkeypatch.setattr(tp, "_get_checks",
                        lambda num, skip_log_fetch=False: json.loads(
                            json.dumps(checks)))
    seen_gh_json = []

    def _fake_gh_json(args, fields=""):
        seen_gh_json.append(args)
        return gh_view or []

    monkeypatch.setattr(tp, "_gh_json", _fake_gh_json)
    result = tp.track(tmp_path)
    return result["prs"][0], seen_gh_json


def test_track_release_only_failure(tmp_path, monkeypatch):
    checks = [
        {"name": "run-selected-tests (vllm@v0.28.0) / cpu-0", "lane":
         "release", "conclusion": "failure", "details_url": "",
         "failure_summary": "TypeError: __init__() missing 1 required "
                            "positional argument: 'max_seq_len_np'"},
        {"name": "run-selected-tests (vllm@" + "a" * 40 + ") / cpu-0",
         "lane": "main", "conclusion": "success", "details_url": ""},
    ]
    record, seen = _track(monkeypatch, tmp_path, checks)
    assert record["ci_conclusion"] == "failure"
    assert record["ci_class"] == "release-only"
    assert record["failing_lanes"] == ["release"]
    assert record["release_lane_failed"] is True
    assert record["main_lane_failed"] is False
    # no conflict probe when checks exist
    assert seen == []


def test_track_both_lanes_and_none(tmp_path, monkeypatch):
    checks = [
        {"name": "run-selected-tests (vllm@v0.28.0) / cpu-0", "lane":
         "release", "conclusion": "failure", "details_url": ""},
        {"name": "run-selected-tests (vllm@" + "a" * 40 + ") / cpu-0",
         "lane": "main", "conclusion": "failure", "details_url": ""},
    ]
    record, _ = _track(monkeypatch, tmp_path, checks)
    assert record["ci_class"] == "both"
    assert record["release_lane_failed"] and record["main_lane_failed"]

    passing = [{"name": "run-selected-tests (vllm@v0.28.0) / cpu-0",
                "lane": "release", "conclusion": "success",
                "details_url": ""}]
    record, _ = _track(monkeypatch, tmp_path, passing)
    assert record["ci_class"] == "none"
    assert record["ci_conclusion"] == "success"


def test_track_conflicting_pr_no_e2e_ran(tmp_path, monkeypatch):
    # CONFLICTING PRs have no merge ref: the pull_request e2e never ran —
    # zero checks must become blocked-conflicting, not signal-less unknown.
    gh_view = {"mergeable": "CONFLICTING",
               "mergeStateStatus": "CONFLICTING"}
    record, seen = _track(monkeypatch, tmp_path, [], gh_view=gh_view)
    assert record["ci_conclusion"] == "blocked-conflicting"
    assert record["ci_class"] == "no-e2e-ran"
    assert record["failing_lanes"] == []
    # the merge state was probed exactly once
    assert seen == [["pr", "view", "16296"]]


def test_track_no_checks_no_conflict_stays_unknown(tmp_path, monkeypatch):
    record, _ = _track(monkeypatch, tmp_path, [])
    assert record["ci_conclusion"] == "unknown"
    assert record["ci_class"] == "none"


def test_load_processed_prs_includes_ci_class(tmp_path):
    results = tmp_path / "vllm-ascend" / "pr_ci_results"
    results.mkdir(parents=True)
    (results / "2026-09-10.json").write_text(json.dumps({
        "prs": [{"pr_number": 1, "ci_conclusion": "failure",
                 "ci_class": "release-only",
                 "checks": [{"failure_summary": "boom"}]}]}))
    processed = tp._load_processed_prs(tmp_path)
    assert processed[1] == {"conclusion": "failure",
                            "ci_class": "release-only"}
    # legacy record without ci_class → empty string forces a re-fetch
    (results / "2026-09-09.json").write_text(json.dumps({
        "prs": [{"pr_number": 2, "ci_conclusion": "failure",
                 "checks": [{"failure_summary": "boom"}]}]}))
    processed = tp._load_processed_prs(tmp_path)
    assert processed[2] == {"conclusion": "failure", "ci_class": ""}


def test_track_refetches_when_ci_class_changes(tmp_path, monkeypatch):
    # A PR recorded before lane tagging existed must be re-fetched (its
    # ci_class is "") — dedup must not swallow the re-classification.
    results = tmp_path / "vllm-ascend" / "pr_ci_results"
    results.mkdir(parents=True)
    (results / "2026-09-10.json").write_text(json.dumps({
        "prs": [{"pr_number": 16296, "ci_conclusion": "failure",
                 "checks": [{"failure_summary": "boom"}]}]}))
    checks = [{"name": "run-selected-tests (vllm@v0.28.0) / cpu-0",
               "lane": "release", "conclusion": "failure",
               "details_url": "", "failure_summary": "TypeError: boom"}]
    pr = {"number": 16296, "title": "adapt to vLLM main (8ec9a87)",
          "createdAt": "2026-09-10T00:00:00Z", "state": "open",
          "url": "https://github.com/x", "labels": [],
          "vllm_commit": "8ec9a87"}
    monkeypatch.setattr(tp, "_gh_pr_list", lambda since: [pr])
    calls = []

    def _fake_get_checks(num, skip_log_fetch=False):
        calls.append(skip_log_fetch)
        return json.loads(json.dumps(checks))

    monkeypatch.setattr(tp, "_get_checks", _fake_get_checks)
    result = tp.track(tmp_path)
    record = result["prs"][0]
    assert record["ci_class"] == "release-only"
    assert record["failing_lanes"] == ["release"]
    # first probe at skip_log_fetch=True, then the full re-fetch
    assert calls == [True, False]
