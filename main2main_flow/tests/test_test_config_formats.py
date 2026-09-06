"""test_config.yaml format drift — vllm-ascend upstream restructured the
file from two YAML docs (module list + meta) into ONE dict doc with
top-level skip_tests / runner_mapping / estimated_times (run 34046694076:
`'str' object has no attribute 'get'` — the parser iterated the dict's
keys as module dicts).  Both parsers must handle BOTH shapes.

estimated_times is the e2e 20min-guarantee's data source: a silent parse
failure here degrades scheduling to default durations.
"""
from pathlib import Path

from main2main_flow.scripts.utils.run_tests import _load_estimated_times
from main2main_flow.scripts.utils.ut_check import _collect_cpu_ut_files

OLD_YAML = """\
- name: core
  skip_tests:
    - tests/ut/core/test_skipme.py
- name: worker
  skip_tests: []
---
estimated_times:
  tests/e2e/pull_request/one_card/test_a.py: 60
  tests/e2e/pull_request/two_card/test_b.py: 300
runner_mapping:
  tests/ut/worker/**: npu
partition: ascend
"""

NEW_YAML = """\
curated_tests:
  smoke:
    - tests/e2e/pull_request/one_card/test_a.py
skip_tests:
  - tests/ut/core/test_skipme.py
accuracy_tests: []
estimated_times:
  tests/e2e/pull_request/one_card/test_a.py: 60
  tests/e2e/pull_request/two_card/test_b.py: 300
runner_mapping:
  tests/e2e/pull_request/one_card: singleNPU
partition: ascend
"""


def _make_repo(tmp_path: Path, yaml_text: str) -> Path:
    repo = tmp_path / "ascend"
    (repo / "tests" / "ut" / "core").mkdir(parents=True)
    (repo / "tests" / "ut" / "worker").mkdir(parents=True)
    (repo / "tests" / "ut" / "core" / "test_keep.py").write_text("")
    (repo / "tests" / "ut" / "core" / "test_skipme.py").write_text("")
    cfg = repo / ".github" / "workflows" / "scripts"
    cfg.mkdir(parents=True)
    (cfg / "test_config.yaml").write_text(yaml_text, encoding="utf-8")
    return repo


def test_estimated_times_old_multidoc(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, OLD_YAML)
    t = _load_estimated_times(repo)
    assert t["tests/e2e/pull_request/one_card/test_a.py"] == 60
    assert t["tests/e2e/pull_request/two_card/test_b.py"] == 300


def test_estimated_times_new_singledoc(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, NEW_YAML)
    t = _load_estimated_times(repo)
    assert t["tests/e2e/pull_request/one_card/test_a.py"] == 60
    assert t["tests/e2e/pull_request/two_card/test_b.py"] == 300


def test_collect_cpu_ut_files_old_format(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, OLD_YAML)
    files = _collect_cpu_ut_files(repo)
    assert "tests/ut/core/test_keep.py" in files
    assert "tests/ut/core/test_skipme.py" not in files
    # runner_mapping routes tests/ut/worker/** to NPU in the old format
    assert "tests/ut/worker/test_keep.py" not in files


def test_collect_cpu_ut_files_new_format(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, NEW_YAML)
    files = _collect_cpu_ut_files(repo)
    assert "tests/ut/core/test_keep.py" in files
    assert "tests/ut/core/test_skipme.py" not in files


def test_estimated_times_missing_file(tmp_path: Path) -> None:
    assert _load_estimated_times(tmp_path / "empty") == {}
