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


DEDICATED_YAML = """\
estimated_times:
  tests/e2e/pull_request/one_card/test_a.py: 690
  tests/e2e/pull_request/two_card/test_c.py: 1020
"""


def _add_dedicated(repo: Path, text: str) -> None:
    (repo / ".github" / "workflows" / "scripts"
     / "estimated_times.yaml").write_text(text, encoding="utf-8")


def test_estimated_times_dedicated_file_only(tmp_path: Path) -> None:
    # Upstream moved the table out of test_config.yaml; the dedicated file
    # alone must still drive scheduling.
    repo = _make_repo(tmp_path, "partition: ascend\n")
    _add_dedicated(repo, DEDICATED_YAML)
    t = _load_estimated_times(repo)
    assert t["tests/e2e/pull_request/one_card/test_a.py"] == 690
    assert t["tests/e2e/pull_request/two_card/test_c.py"] == 1020


def test_estimated_times_dedicated_file_wins(tmp_path: Path) -> None:
    # Both tables present: legacy-only keys survive, conflicting keys take
    # the dedicated file's value.
    repo = _make_repo(tmp_path, NEW_YAML)
    _add_dedicated(repo, DEDICATED_YAML)
    t = _load_estimated_times(repo)
    assert t["tests/e2e/pull_request/one_card/test_a.py"] == 690
    assert t["tests/e2e/pull_request/two_card/test_b.py"] == 300
    assert t["tests/e2e/pull_request/two_card/test_c.py"] == 1020


def test_estimated_times_corrupt_dedicated_keeps_legacy(tmp_path: Path) -> None:
    # A corrupt file must not discard the other table (that is exactly the
    # silent-degradation failure this loader was fixed for).
    repo = _make_repo(tmp_path, NEW_YAML)
    _add_dedicated(repo, "estimated_times: [unclosed\n")
    t = _load_estimated_times(repo)
    assert t["tests/e2e/pull_request/one_card/test_a.py"] == 60
    assert t["tests/e2e/pull_request/two_card/test_b.py"] == 300


def test_collect_cpu_ut_files_matches_pytest_order(tmp_path: Path) -> None:
    """The CPU-UT batch must run in pytest's collection order.

    os.walk yields root-level files before subdirectories, inverting
    pytest's name-interleaved order.  On that inverted order
    ``test_logger.py`` runs early and leaks a file-logging handler bound
    to its deleted TemporaryDirectory; a later ``runtime_config/`` test
    reconfigures logging and every subsequent log emit re-opens the dead
    stream (parity run 38405850804: 371 cascade violations on os.walk
    order vs 1 failure in pytest's order on the same tree).
    """
    repo = tmp_path / "ascend"
    (repo / "tests" / "ut" / "runtime_config").mkdir(parents=True)
    (repo / "tests" / "ut" / "test_logger.py").write_text("")
    (repo / "tests" / "ut" / "runtime_config" / "test_reconfigurer.py").write_text("")
    cfg = repo / ".github" / "workflows" / "scripts"
    cfg.mkdir(parents=True)
    (cfg / "test_config.yaml").write_text(NEW_YAML, encoding="utf-8")

    files = _collect_cpu_ut_files(repo)

    # pytest order: directory entries interleave with files by name, so
    # runtime_config/ sorts before test_logger.py — NOT os.walk's
    # root-files-first order.
    assert files == [
        "tests/ut/runtime_config/test_reconfigurer.py",
        "tests/ut/test_logger.py",
    ]
