"""Extended e2e case resolution: tree scan, fixed/blocklist/skip subtraction,
import-closure tiering, and ordering.

The 2026-09-15 run shipped PR 16575 green on the fixed 25-case set while
upstream CI failed legs the fixed set never touched — the extended phase
runs what upstream PR CI would (tree scan of tests/e2e/pull_request minus
skip_tests, NOT main2main_tests.json which is an 18-entry fixed-set subset
whose subtraction would be empty)."""
from __future__ import annotations

from pathlib import Path

import yaml

from main2main_flow.scripts.utils import extended_e2e as ee
from main2main_flow.scripts.utils.extended_e2e import (
    changed_module_names,
    covered_by_fixed,
    extract_imported_modules,
    load_upstream_skip_tests,
    order_cases,
    partition_by_import_closure,
    resolve_extended_cases,
    scan_e2e_pull_request_files,
)


def _make_ascend(tmp_path: Path, files: list[str]) -> str:
    """Minimal vllm-ascend-like tree: test files + test_config.yaml."""
    ascend = tmp_path / "ascend"
    for rel in files:
        p = ascend / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
    return str(ascend)


def test_scan_finds_only_test_files_under_pull_request(tmp_path):
    files = [
        "tests/e2e/pull_request/one_card/test_basic.py",
        "tests/e2e/pull_request/one_card/basic_helper.py",  # not test_*
        "tests/e2e/pull_request/one_card/conftest.py",
        "tests/e2e/pull_request/two_card/spec_decode/test_spec_decode.py",
        "tests/ut/ops/test_rope.py",  # outside pull_request root
    ]
    ascend = _make_ascend(tmp_path, files)
    found = scan_e2e_pull_request_files(ascend)
    assert found == [
        "tests/e2e/pull_request/one_card/test_basic.py",
        "tests/e2e/pull_request/two_card/spec_decode/test_spec_decode.py",
    ]


def test_scan_empty_when_root_missing(tmp_path):
    assert scan_e2e_pull_request_files(str(tmp_path)) == []


def test_covered_by_fixed_file_and_node_semantics():
    fixed = [
        "tests/e2e/pull_request/one_card/test_sampler.py",
        "tests/e2e/pull_request/one_card/model_runner_v2/test_basic.py::test_mtp_spec_decoding",
    ]
    # File candidate: file-level entry covers; node entry covers its file too.
    assert covered_by_fixed("tests/e2e/pull_request/one_card/test_sampler.py", fixed)
    assert covered_by_fixed(
        "tests/e2e/pull_request/one_card/model_runner_v2/test_basic.py", fixed)
    # Unrelated file not covered.
    assert not covered_by_fixed(
        "tests/e2e/pull_request/one_card/test_vlm.py", fixed)
    # Node candidate: exact node or file-level entry covers.
    assert covered_by_fixed(
        "tests/e2e/pull_request/one_card/model_runner_v2/test_basic.py::test_mtp_spec_decoding",
        fixed)
    assert not covered_by_fixed(
        "tests/e2e/pull_request/one_card/model_runner_v2/test_basic.py::other_node",
        fixed)


def test_load_upstream_skip_tests(tmp_path):
    cfg = tmp_path / ".github/workflows/scripts"
    cfg.mkdir(parents=True)
    (cfg / "test_config.yaml").write_text(yaml.safe_dump({
        "skip_tests": ["tests/e2e/pull_request/one_card/test_x.py"],
        "estimated_times": {"tests/e2e/pull_request/one_card/test_x.py": 60},
    }), encoding="utf-8")
    assert load_upstream_skip_tests(str(tmp_path)) == {
        "tests/e2e/pull_request/one_card/test_x.py"}


def test_load_upstream_skip_tests_unparsable_is_empty(tmp_path):
    cfg = tmp_path / ".github/workflows/scripts"
    cfg.mkdir(parents=True)
    (cfg / "test_config.yaml").write_text(":::: [broken", encoding="utf-8")
    assert load_upstream_skip_tests(str(tmp_path)) == set()


def test_extract_imported_modules_forms(tmp_path):
    p = tmp_path / "test_a.py"
    p.write_text(
        "import vllm_ascend.ops.rope\n"
        "from vllm_ascend.worker.spec_decode import dflash\n"
        "from vllm_ascend import platform\n"
        "from tests.e2e.conftest import VllmRunner\n"
        "import os\n"
        "from . import sibling\n"
        "from vllm_ascend.worker.spec_decode import *\n",
        encoding="utf-8")
    mods = extract_imported_modules(p)
    assert "vllm_ascend.ops.rope" in mods
    assert "vllm_ascend.worker.spec_decode" in mods
    assert "vllm_ascend.worker.spec_decode.dflash" in mods
    assert "vllm_ascend.platform" in mods
    assert "tests.e2e.conftest" in mods
    assert "os" not in mods
    # relative import and star import add nothing spurious
    assert not any(m.endswith(".sibling") for m in mods)


def test_extract_imported_modules_unparsable_is_empty(tmp_path):
    p = tmp_path / "test_b.py"
    p.write_text("def broken(:\n", encoding="utf-8")
    assert extract_imported_modules(p) == set()
    assert extract_imported_modules(tmp_path / "missing.py") == set()


def test_changed_module_names_maps_paths_and_parents():
    mods = changed_module_names([
        "vllm_ascend/ops/rope.py",
        "vllm_ascend/worker/__init__.py",
        ".github/workflows/pr_test.yaml",
        "docs/guide.md",
        "tests/ut/ops/test_rope.py",
    ])
    assert "vllm_ascend.ops.rope" in mods
    assert "vllm_ascend.ops" in mods
    assert "vllm_ascend" not in mods  # root excluded: one submodule must
    # not tier every test importing any vllm_ascend.* module
    assert "vllm_ascend.worker" in mods  # __init__ maps to the package
    assert "tests.ut.ops.test_rope" in mods
    assert not any(m.startswith(".github") or m.startswith("docs") for m in mods)


def test_partition_tier1_direct_and_package_hits(tmp_path):
    ascend = _make_ascend(tmp_path, [
        "tests/e2e/pull_request/one_card/test_direct.py",
        "tests/e2e/pull_request/one_card/test_pkg.py",
        "tests/e2e/pull_request/one_card/test_other.py",
    ])
    (tmp_path / "ascend/tests/e2e/pull_request/one_card/test_direct.py"
     ).write_text("from vllm_ascend.ops.rope import RoPE\n", encoding="utf-8")
    (tmp_path / "ascend/tests/e2e/pull_request/one_card/test_pkg.py"
     ).write_text("from vllm_ascend.ops import rope\n", encoding="utf-8")
    (tmp_path / "ascend/tests/e2e/pull_request/one_card/test_other.py"
     ).write_text("import vllm_ascend.platform\n", encoding="utf-8")
    changed = changed_module_names(["vllm_ascend/ops/rope.py"])
    cases = scan_e2e_pull_request_files(ascend)
    tier1, tier2 = partition_by_import_closure(cases, changed, ascend)
    assert tier1 == ["tests/e2e/pull_request/one_card/test_direct.py",
                     "tests/e2e/pull_request/one_card/test_pkg.py"]
    assert tier2 == ["tests/e2e/pull_request/one_card/test_other.py"]
    # No diff knowledge -> everything tier2.
    t1, t2 = partition_by_import_closure(cases, set(), ascend)
    assert (t1, t2) == ([], cases)


def test_order_cases_tier1_first_then_time_ascending():
    times = {"tests/e2e/a.py": 300, "tests/e2e/b.py": 60,
             "tests/e2e/c.py": 120}
    cases = ["tests/e2e/a.py", "tests/e2e/b.py", "tests/e2e/c.py",
             "tests/e2e/unknown.py"]
    ordered = order_cases(cases, tier1=["tests/e2e/a.py"], estimated_times=times)
    # tier1 first; within tier ascending estimate; unknown estimates last.
    assert ordered[0] == "tests/e2e/a.py"
    assert ordered[1:] == ["tests/e2e/b.py", "tests/e2e/c.py",
                           "tests/e2e/unknown.py"]
    assert ee._DEFAULT_ESTIMATED_SECONDS > 0


def test_resolve_extended_cases_subtraction_chain(tmp_path):
    files = [
        "tests/e2e/pull_request/one_card/test_free.py",
        "tests/e2e/pull_request/one_card/test_skipped.py",
        "tests/e2e/pull_request/one_card/test_fixed.py",
        "tests/e2e/pull_request/one_card/test_blocked.py",
    ]
    ascend = _make_ascend(tmp_path, files)
    cfg = tmp_path / "ascend/.github/workflows/scripts"
    cfg.mkdir(parents=True)
    (cfg / "test_config.yaml").write_text(yaml.safe_dump({
        "skip_tests": ["tests/e2e/pull_request/one_card/test_skipped.py"],
    }), encoding="utf-8")
    fixed = ["tests/e2e/pull_request/one_card/test_fixed.py::node"]
    blocked = ["tests/e2e/pull_request/one_card/test_blocked.py"]
    out = resolve_extended_cases(ascend, fixed, blocked)
    assert out["cases"] == ["tests/e2e/pull_request/one_card/test_free.py"]
    assert out["dropped_skip"] == [
        "tests/e2e/pull_request/one_card/test_skipped.py"]
    assert out["dropped_missing"] == []
    assert out["source"] == "tree-scan tests/e2e/pull_request"
    # include_skipped keeps the upstream-skipped file.
    out2 = resolve_extended_cases(ascend, fixed, blocked, include_skipped=True)
    assert "tests/e2e/pull_request/one_card/test_skipped.py" in out2["cases"]
    assert out2["dropped_skip"] == []


def test_resolve_extended_cases_override_and_missing(tmp_path):
    ascend = _make_ascend(tmp_path, ["tests/e2e/pull_request/one_card/a.py"])
    out = resolve_extended_cases(
        ascend, [], [],
        override=["tests/e2e/pull_request/one_card/a.py",
                  "tests/e2e/pull_request/one_card/ghost.py"])
    assert out["cases"] == ["tests/e2e/pull_request/one_card/a.py"]
    assert out["dropped_missing"] == [
        "tests/e2e/pull_request/one_card/ghost.py"]
    assert out["source"] == "override"


def test_prune_fixed_set_drops_drift_skip_missing(tmp_path):
    from main2main_flow.scripts.utils.extended_e2e import prune_fixed_set

    files = [
        "tests/e2e/pull_request/one_card/test_free.py",
        "tests/e2e/pull_request/one_card/test_drift.py",   # allowlist covers
        "tests/e2e/pull_request/one_card/test_skipped.py",  # upstream skip
    ]
    ascend = _make_ascend(tmp_path, files)
    cfg = tmp_path / "ascend/.github/workflows/scripts"
    cfg.mkdir(parents=True)
    (cfg / "test_config.yaml").write_text(yaml.safe_dump({
        "skip_tests": ["tests/e2e/pull_request/one_card/test_skipped.py"],
    }), encoding="utf-8")
    # the stale curated list still names a file the tree no longer has
    candidates = files + ["tests/e2e/pull_request/one_card/test_ghost.py"]
    out = prune_fixed_set(
        ascend,
        candidates,
        ["tests/e2e/pull_request/one_card/test_drift.py::some_node"])
    # node-level allowlist coverage counts as drift — those cases already
    # ran every step
    assert out["cases"] == [
        "tests/e2e/pull_request/one_card/test_free.py"]
    assert out["dropped_fixed"] == [
        "tests/e2e/pull_request/one_card/test_drift.py"]
    assert out["dropped_skip"] == [
        "tests/e2e/pull_request/one_card/test_skipped.py"]
    assert out["dropped_missing"] == [
        "tests/e2e/pull_request/one_card/test_ghost.py"]


def test_prune_fixed_set_keeps_clean_list(tmp_path):
    from main2main_flow.scripts.utils.extended_e2e import prune_fixed_set

    ascend = _make_ascend(tmp_path, [
        "tests/e2e/pull_request/one_card/test_a.py"])
    out = prune_fixed_set(
        ascend, ["tests/e2e/pull_request/one_card/test_a.py"], [])
    assert out["cases"] == ["tests/e2e/pull_request/one_card/test_a.py"]
    assert not any(out[k] for k in
                   ("dropped_fixed", "dropped_skip", "dropped_310p",
                    "dropped_missing"))


def test_prune_fixed_set_drops_310p_structurally(tmp_path):
    # _310p suites never run on the a3-16 pool (user 2026-09-15) — the
    # guard is structural, so even an explicit MAIN2MAIN_EXTENDED_TEST_
    # CASES override cannot schedule them.
    from main2main_flow.scripts.utils.extended_e2e import prune_fixed_set

    ascend = _make_ascend(tmp_path, [
        "tests/e2e/pull_request/one_card/_310p/test_vl_model_310p.py",
        "tests/e2e/pull_request/one_card/test_a.py"])
    out = prune_fixed_set(
        ascend,
        ["tests/e2e/pull_request/one_card/_310p/test_vl_model_310p.py",
         "tests/e2e/pull_request/one_card/test_a.py"],
        [])
    assert out["cases"] == ["tests/e2e/pull_request/one_card/test_a.py"]
    assert out["dropped_310p"] == [
        "tests/e2e/pull_request/one_card/_310p/test_vl_model_310p.py"]


def test_resolve_extended_cases_drops_310p_from_tree_scan(tmp_path):
    ascend = _make_ascend(tmp_path, [
        "tests/e2e/pull_request/one_card/_310p/test_vl_model_310p.py",
        "tests/e2e/pull_request/one_card/test_free.py"])
    out = resolve_extended_cases(ascend, [], [])
    assert out["cases"] == ["tests/e2e/pull_request/one_card/test_free.py"]
    assert out["dropped_310p"] == [
        "tests/e2e/pull_request/one_card/_310p/test_vl_model_310p.py"]
