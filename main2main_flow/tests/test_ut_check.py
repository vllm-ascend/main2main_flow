"""Persistent UT venv, shared env construction, and full-log persistence
(the adapter's closed verify loop — run 33976675052 step-1 died editing
blind because the venv was destroyed and stdout never persisted)."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from main2main_flow.scripts.utils import ut_check
from main2main_flow.scripts.utils.ut_check import (
    _build_ut_env,
    _ensure_ut_venv,
    check_ut,
    compute_base_ut_baseline,
)


def _flow_root() -> str:
    return str(Path(ut_check.__file__).resolve().parents[3])


def test_build_ut_env_pure_cpu_and_paths(tmp_path):
    repo = tmp_path / "ascend"
    vllm = tmp_path / "vllm"
    fake_bin = tmp_path / "fakebin"
    for d in (repo, vllm, fake_bin):
        d.mkdir()
    env = _build_ut_env(repo, vllm, fake_bin)
    assert env["PYTHONPATH"].split(":")[:2] == [str(repo), str(vllm)]
    # `python -m pytest` prepends the graded tree (cwd) ahead of
    # PYTHONPATH; PYTHONSAFEPATH gives -m the console-script sys.path
    # semantics so the pure-CPU view cannot be shadowed by the tree's
    # own _build_info (run 38421012498).
    assert env["PYTHONSAFEPATH"] == "1"
    assert env["PYTHONPATH"].endswith(_flow_root())
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["ASCEND_RT_VISIBLE_DEVICES"] == ""
    assert "CUDA_VISIBLE_DEVICES" not in env
    assert env["TORCH_DEVICE_BACKEND_AUTOLOAD"] == "0"
    assert env["VLLM_WORKER_MULTIPROC_METHOD"] == "spawn"
    assert env["PATH"].startswith(str(fake_bin))


def test_build_ut_env_scrubs_production_csrc_cache_leak(
        tmp_path, monkeypatch):
    # Production main2main's csrc-l1-restore exports these into GITHUB_ENV;
    # upstream cpu-UT never sees them (all csrc steps gated npu != cpu) and
    # VLLM_ASCEND_BUILD_CACHE_DIR defeats the CMake default that
    # test_build_cache asserts (run 38383045302 step-1).
    repo = tmp_path / "ascend"
    repo.mkdir()
    monkeypatch.setenv("VLLM_ASCEND_BUILD_CACHE_DIR",
                       str(tmp_path / "vllm-ascend-incremental-build-cache"))
    monkeypatch.setenv("VLLM_ASCEND_BUILD_CACHE_EVENT_LOG",
                       str(tmp_path / "csrc-l1-job.jsonl"))
    env = _build_ut_env(repo, tmp_path / "vllm", tmp_path / "fakebin")
    assert "VLLM_ASCEND_BUILD_CACHE_DIR" not in env
    assert "VLLM_ASCEND_BUILD_CACHE_EVENT_LOG" not in env


def test_ensure_ut_venv_reuses_matching_marker(tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    py.write_text("")
    (venv / "m2m_meta.json").write_text(json.dumps({"numpy_spec": "==1.26.4"}))

    # reuse still probes the actual numpy (run 38220121641: marker stayed
    # healthy while system numpy drifted under the venv) — but must NOT
    # recreate
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "import numpy" in str(cmd[-1]):
            return SimpleNamespace(returncode=0, stdout="1.26.4\n", stderr="")
        raise AssertionError("venv creation on the reuse path")

    monkeypatch.setattr(ut_check.subprocess, "run", _fake_run)
    monkeypatch.setenv("MAIN2MAIN_UT_VENV", str(venv))
    got_dir, got_py = _ensure_ut_venv("==1.26.4")
    assert got_dir == venv
    assert got_py == str(py)
    assert calls


def test_ensure_ut_venv_recreates_when_numpy_drifted(tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("")
    (venv / "m2m_meta.json").write_text(json.dumps({"numpy_spec": "==1.26.4"}))

    calls = []
    probes = ["2.3.5", "1.26.4"]  # reuse probe drifted, post-install healthy

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "import numpy" in str(cmd[-1]):
            v = probes.pop(0) if len(probes) > 1 else probes[0]
            return SimpleNamespace(returncode=0, stdout=f"{v}\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    ok = SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(ut_check.subprocess, "run", _fake_run)
    pip_args = []
    monkeypatch.setattr(ut_check, "pip_install_with_fallback",
                        lambda py, args: pip_args.append(args) or ok)
    monkeypatch.setenv("MAIN2MAIN_UT_VENV", str(venv))
    got_dir, got_py = _ensure_ut_venv("==1.26.4")
    assert got_dir == venv
    assert got_py == str(venv / "bin" / "python")
    assert any("venv" in str(c) for c in calls)  # recreated, not reused
    assert any("--ignore-installed" in a for a in pip_args)
    assert json.loads((venv / "m2m_meta.json").read_text())["numpy_spec"] \
        == "==1.26.4"


def test_ensure_ut_venv_recreates_on_spec_mismatch(tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("")
    (venv / "m2m_meta.json").write_text(json.dumps({"numpy_spec": "==1.24.0"}))

    ok = SimpleNamespace(returncode=0, stdout="", stderr="")
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        if "import numpy" in str(cmd[-1]):
            return SimpleNamespace(returncode=0, stdout="1.26.4\n", stderr="")
        return ok

    monkeypatch.setattr(ut_check.subprocess, "run", _fake_run)
    monkeypatch.setattr(ut_check, "pip_install_with_fallback",
                        lambda *a, **k: ok)
    monkeypatch.setenv("MAIN2MAIN_UT_VENV", str(venv))
    got_dir, got_py = _ensure_ut_venv("==1.26.4")
    assert got_dir == venv
    assert got_py == str(venv / "bin" / "python")
    assert any("venv" in str(c) for c in calls)  # recreated, not reused
    assert json.loads((venv / "m2m_meta.json").read_text())["numpy_spec"] \
        == "==1.26.4"


def test_check_ut_persists_full_log_and_returns_keys(tmp_path, monkeypatch):
    repo = tmp_path / "ascend"
    repo.mkdir()
    vllm = tmp_path / "vllm"
    vllm.mkdir()
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    py.write_text("")

    monkeypatch.setattr(ut_check, "_collect_cpu_ut_files",
                        lambda repo: ["tests/ut/test_a.py"])
    monkeypatch.setattr(ut_check, "_ensure_ut_venv",
                        lambda spec: (venv, str(py)))
    ok = SimpleNamespace(returncode=0, stdout="1 passed in 0.01s\n",
                         stderr="")
    seen_cmds = []

    def _fake_run(cmd, **kwargs):
        seen_cmds.append((cmd, kwargs))
        return ok

    monkeypatch.setattr(ut_check.subprocess, "run", _fake_run)

    result = check_ut(repo, vllm_path=vllm)
    assert result["violations"] == []
    assert result["log_path"] == str(venv / "ut_full.log")
    assert result["venv_python"] == str(py)
    log_text = Path(result["log_path"]).read_text()
    assert "1 passed in 0.01s" in log_text
    assert log_text.startswith("# pre_ci UT full log")
    # the batch runs through the venv python with the namespace plugin
    cmd = seen_cmds[0][0]
    assert cmd[:3] == [str(py), "-m", "pytest"]
    assert "main2main_flow.scripts.utils.ut_namespace" in cmd


def test_check_ut_venv_python_empty_on_system_fallback(tmp_path, monkeypatch):
    repo = tmp_path / "ascend"
    repo.mkdir()
    vllm = tmp_path / "vllm"
    vllm.mkdir()
    monkeypatch.setattr(ut_check, "_collect_cpu_ut_files",
                        lambda repo: ["tests/ut/test_a.py"])
    monkeypatch.setattr(ut_check, "_ensure_ut_venv", lambda spec: (None, ""))
    ok = SimpleNamespace(returncode=0, stdout="1 passed in 0.01s\n",
                         stderr="")
    monkeypatch.setattr(ut_check.subprocess, "run", lambda *a, **k: ok)
    result = check_ut(repo, vllm_path=vllm)
    assert result["log_path"]  # still persisted (workspace dir)
    assert result["venv_python"] == ""


def test_check_ut_dual_version_signature() -> None:
    import inspect
    params = inspect.signature(check_ut).parameters
    assert "vllm_release_path" in params
    assert "release_tag" in params


def _dual_lane_setup(tmp_path: Path, monkeypatch, outs_by_lane: dict):
    """Two-batch rig: per-lane stdout keyed on VLLM_VERSION presence."""
    repo = tmp_path / "ascend"
    repo.mkdir()
    vllm = tmp_path / "vllm"
    vllm.mkdir()
    release = tmp_path / "vllm-release"
    release.mkdir()
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    py.write_text("")

    monkeypatch.setattr(ut_check, "_collect_cpu_ut_files",
                        lambda repo: ["tests/ut/test_a.py"])
    monkeypatch.setattr(ut_check, "_ensure_ut_venv",
                        lambda spec: (venv, str(py)))
    seen: list[tuple[list, dict]] = []

    def _fake_run(cmd, **kwargs):
        seen.append((cmd, kwargs))
        env = kwargs.get("env", {})
        is_release = bool(env.get("VLLM_VERSION"))
        out = outs_by_lane["release"] if is_release else outs_by_lane["main"]
        rc_key = "release_rc" if is_release else "main_rc"
        default_rc = 0 if out.endswith("passed in 0.01s\n") else 1
        return SimpleNamespace(returncode=outs_by_lane.get(rc_key, default_rc),
                               stdout=out, stderr="")

    monkeypatch.setattr(ut_check.subprocess, "run", _fake_run)
    return repo, vllm, release, seen


def test_check_ut_release_batch_env_and_exclude(tmp_path, monkeypatch):
    repo, vllm, release, seen = _dual_lane_setup(
        tmp_path, monkeypatch,
        {"main": "1 passed in 0.01s\n", "release": "1 passed in 0.01s\n",
         "rc": 0})
    result = check_ut(repo, vllm_path=vllm, vllm_release_path=release,
                      release_tag="v0.28.0")
    assert result["violations"] == []
    assert len(seen) == 2  # main batch + release batch, no isolated files
    main_env = seen[0][1]["env"]
    release_env = seen[1][1]["env"]
    assert "VLLM_VERSION" not in main_env
    assert release_env["VLLM_VERSION"] == "0.28.0"  # v-prefix stripped
    main_k = seen[0][0][seen[0][0].index("-k") + 1]
    release_k = seen[1][0][seen[1][0].index("-k") + 1]
    assert "test_vllm_version_is" not in main_k
    assert "and not test_vllm_version_is" in release_k
    # Both lanes land in ONE log; release section is labeled.
    log_text = Path(result["log_path"]).read_text()
    assert "release-lane batch" in log_text
    assert "VLLM_VERSION=0.28.0" in log_text


def test_check_ut_release_baseline_filters_violations(tmp_path, monkeypatch):
    fail_out = ("1 failed, 0 passed in 0.01s\n"
                "FAILED tests/ut/test_a.py::test_b - AssertionError\n")
    repo, vllm, release, seen = _dual_lane_setup(
        tmp_path, monkeypatch,
        {"main": "1 passed in 0.01s\n", "release": fail_out,
         "release_rc": 1})
    monkeypatch.setattr(ut_check, "_load_release_baseline",
                        lambda: {"tests/ut/test_a.py::test_b"})
    result = check_ut(repo, vllm_path=vllm, vllm_release_path=release,
                      release_tag="0.28.0")
    # Baseline-matched: reported in detail + log, never blocks.
    assert result["violations"] == []
    assert "matched the known baseline" in result["detail"]
    log_text = Path(result["log_path"]).read_text()
    assert "FAILED tests/ut/test_a.py::test_b" in log_text


def test_check_ut_release_baseline_disabled_blocks(tmp_path, monkeypatch):
    fail_out = ("1 failed, 0 passed in 0.01s\n"
                "FAILED tests/ut/test_a.py::test_b - AssertionError\n")
    repo, vllm, release, seen = _dual_lane_setup(
        tmp_path, monkeypatch,
        {"main": "1 passed in 0.01s\n", "release": fail_out,
         "release_rc": 1})
    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text(
        json.dumps({"excluded": ["tests/ut/test_a.py::test_b"]}))
    monkeypatch.setattr(ut_check, "_RELEASE_UT_BASELINE_FILE", baseline_file)
    monkeypatch.setenv("MAIN2MAIN_RELEASE_UT_BASELINE", "0")
    result = check_ut(repo, vllm_path=vllm, vllm_release_path=release,
                      release_tag="0.28.0")
    assert any("test_a.py::test_b" in v for v in result["violations"])


def test_check_ut_no_release_path_single_batch(tmp_path, monkeypatch):
    repo, vllm, release, seen = _dual_lane_setup(
        tmp_path, monkeypatch,
        {"main": "1 passed in 0.01s\n", "release": "1 passed in 0.01s\n",
         "rc": 0})
    result = check_ut(repo, vllm_path=vllm)
    assert result["violations"] == []
    assert len(seen) == 1  # main batch only


def test_check_ut_release_collection_error_parsed(tmp_path, monkeypatch):
    """Nodeless `ERROR <file>.py` (collection error, no ::node) must parse
    into a blocking violation — run 34583706211's invisible final blocker."""
    fail_out = ("1 error in 0.01s\n"
                "ERROR tests/ut/test_a.py - ModuleNotFoundError: "
                "No module named 'vllm.foo'\n")
    repo, vllm, release, seen = _dual_lane_setup(
        tmp_path, monkeypatch,
        {"main": "1 passed in 0.01s\n", "release": fail_out,
         "release_rc": 1})
    result = check_ut(repo, vllm_path=vllm, vllm_release_path=release,
                      release_tag="0.28.0")
    assert len(result["violations"]) == 1
    v = result["violations"][0]
    assert v.startswith("[0.28.0] ERROR tests/ut/test_a.py")
    assert "ModuleNotFoundError" in v
    # Excerpt carries the traceback context, not just the summary line.
    assert "No module named 'vllm.foo'" in v


def test_check_ut_unparseable_fallback_prefers_exception_lines(
        tmp_path, monkeypatch):
    """No parseable lines at all → the fallback excerpt must come from the
    pytest `E   ...` exception lines, not the stdout (warnings-summary)
    tail that blinded run 34583706211's final gate round."""
    fail_out = ("2 warnings in 0.01s\n"
                "Traceback (most recent call last):\n"
                "  File \"conftest.py\", line 3, in <module>\n"
                "E   ModuleNotFoundError: No module named 'vllm.foo'\n"
                "  warning: asyncio_default_fixture_loop_scope\" is unset.\n")
    repo, vllm, release, seen = _dual_lane_setup(
        tmp_path, monkeypatch,
        {"main": "1 passed in 0.01s\n", "release": fail_out,
         "release_rc": 1})
    result = check_ut(repo, vllm_path=vllm, vllm_release_path=release,
                      release_tag="0.28.0")
    assert len(result["violations"]) == 1
    v = result["violations"][0]
    assert v.startswith("[0.28.0] batch: exit=1")
    assert "ModuleNotFoundError: No module named 'vllm.foo'" in v
    assert "fixture_loop_scope" not in v


def test_unparseable_evidence_tail_fallback():
    # No E-lines, no traceback → degrades to the raw tail.
    out = "nothing useful here\njust a tail\n"
    assert ut_check._unparseable_evidence(out) == out[-500:]
    # An E-line anchor wins over the tail.
    out2 = ("warnings summary\n"
            "E   OSError: died at startup\n"
            "more tail text that should not appear\n")
    ev = ut_check._unparseable_evidence(out2)
    assert "OSError: died at startup" in ev
    assert "more tail text" not in ev


def test_load_release_baseline_from_file(tmp_path, monkeypatch):
    baseline_file = tmp_path / "baseline.json"
    baseline_file.write_text(json.dumps(
        {"excluded": ["tests/ut/x.py::test_a", "tests/ut/y.py::test_b"]}))
    monkeypatch.setattr(ut_check, "_RELEASE_UT_BASELINE_FILE", baseline_file)
    assert ut_check._load_release_baseline() == {
        "tests/ut/x.py::test_a", "tests/ut/y.py::test_b"}
    # Missing file → empty baseline, never an exception.
    monkeypatch.setattr(ut_check, "_RELEASE_UT_BASELINE_FILE",
                        tmp_path / "nope.json")
    assert ut_check._load_release_baseline() == set()


# --- main-lane base-tree baseline (run 38383045302) -------------------------
# A fresh-mode step can start from a vllm base whose CPU-UT batch already
# fails; step-1 burned all 5 attempts on failures the adapter proved
# pre-existing.  compute_base_ut_baseline snapshots those node IDs at
# kickoff (vllm@base + ascend@kickoff); check_ut then reports them but
# never blocks on them.

_MAIN_FAIL_OUT = ("1 failed, 0 passed in 0.01s\n"
                  "FAILED tests/ut/test_a.py::test_b - AssertionError\n")


def test_check_ut_main_baseline_exempts(tmp_path, monkeypatch):
    repo, vllm, release, seen = _dual_lane_setup(
        tmp_path, monkeypatch,
        {"main": _MAIN_FAIL_OUT, "release": "1 passed in 0.01s\n"})
    baseline_file = tmp_path / "base_ut_failures.json"
    baseline_file.write_text(
        json.dumps({"excluded": ["tests/ut/test_a.py::test_b"]}))
    monkeypatch.setattr(ut_check, "_MAIN_UT_BASELINE_FILE", baseline_file)
    result = check_ut(repo, vllm_path=vllm)
    # Baseline-matched: reported in detail + log, never blocks.
    assert result["violations"] == []
    assert "matched the known baseline" in result["detail"]
    # ... but still reported as a failed node (the snapshot's raw truth).
    assert result["failed_nodes"] == ["tests/ut/test_a.py::test_b"]
    log_text = Path(result["log_path"]).read_text()
    assert "FAILED tests/ut/test_a.py::test_b" in log_text


def test_check_ut_main_baseline_whole_file_match(tmp_path, monkeypatch):
    repo, vllm, release, seen = _dual_lane_setup(
        tmp_path, monkeypatch,
        {"main": _MAIN_FAIL_OUT, "release": "1 passed in 0.01s\n"})
    baseline_file = tmp_path / "base_ut_failures.json"
    baseline_file.write_text(json.dumps({"excluded": ["tests/ut/test_a.py"]}))
    monkeypatch.setattr(ut_check, "_MAIN_UT_BASELINE_FILE", baseline_file)
    result = check_ut(repo, vllm_path=vllm)
    assert result["violations"] == []
    assert "matched the known baseline" in result["detail"]


def test_check_ut_main_baseline_disabled_blocks(tmp_path, monkeypatch):
    repo, vllm, release, seen = _dual_lane_setup(
        tmp_path, monkeypatch,
        {"main": _MAIN_FAIL_OUT, "release": "1 passed in 0.01s\n"})
    baseline_file = tmp_path / "base_ut_failures.json"
    baseline_file.write_text(
        json.dumps({"excluded": ["tests/ut/test_a.py::test_b"]}))
    monkeypatch.setattr(ut_check, "_MAIN_UT_BASELINE_FILE", baseline_file)
    monkeypatch.setenv("MAIN2MAIN_BASE_UT_BASELINE", "0")
    result = check_ut(repo, vllm_path=vllm)
    assert any("test_a.py::test_b" in v for v in result["violations"])


def test_check_ut_snapshot_mode_overrides_file(tmp_path, monkeypatch):
    # explicit known_failures=set() must win over the workspace file —
    # that is exactly how compute_base_ut_baseline takes the snapshot.
    repo, vllm, release, seen = _dual_lane_setup(
        tmp_path, monkeypatch,
        {"main": _MAIN_FAIL_OUT, "release": "1 passed in 0.01s\n"})
    baseline_file = tmp_path / "base_ut_failures.json"
    baseline_file.write_text(
        json.dumps({"excluded": ["tests/ut/test_a.py::test_b"]}))
    monkeypatch.setattr(ut_check, "_MAIN_UT_BASELINE_FILE", baseline_file)
    result = check_ut(repo, vllm_path=vllm, known_failures=set())
    assert any("test_a.py::test_b" in v for v in result["violations"])
    assert result["failed_nodes"] == ["tests/ut/test_a.py::test_b"]


def test_compute_base_ut_baseline_writes_and_exempts(tmp_path, monkeypatch):
    repo = tmp_path / "ascend"
    repo.mkdir()
    vllm = tmp_path / "vllm"
    (vllm / ".git").mkdir(parents=True)
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    py.write_text("")
    baseline_file = tmp_path / "base_ut_failures.json"
    monkeypatch.setattr(ut_check, "_MAIN_UT_BASELINE_FILE", baseline_file)
    monkeypatch.setattr(ut_check, "_collect_cpu_ut_files",
                        lambda r: ["tests/ut/test_a.py"])
    monkeypatch.setattr(ut_check, "_ensure_ut_venv",
                        lambda spec: (venv, str(py)))

    head = SimpleNamespace(returncode=0, stdout="abc123def456\n", stderr="")
    batch = SimpleNamespace(returncode=1, stdout=_MAIN_FAIL_OUT, stderr="")

    def _fake_run(cmd, **kwargs):
        if cmd[0] == "git":
            return head
        return batch

    monkeypatch.setattr(ut_check.subprocess, "run", _fake_run)

    record = compute_base_ut_baseline(repo, vllm)
    assert record["excluded"] == ["tests/ut/test_a.py::test_b"]
    assert record["recorded_head"] == "abc123def456"
    on_disk = json.loads(baseline_file.read_text())
    assert on_disk["excluded"] == ["tests/ut/test_a.py::test_b"]
    # end-to-end: the run's later check_ut calls now exempt via the file
    result = check_ut(repo, vllm_path=vllm)
    assert result["violations"] == []
    assert "matched the known baseline" in result["detail"]


def test_compute_base_ut_baseline_env_disabled(tmp_path, monkeypatch):
    repo = tmp_path / "ascend"
    repo.mkdir()
    vllm = tmp_path / "vllm"
    (vllm / ".git").mkdir(parents=True)
    baseline_file = tmp_path / "base_ut_failures.json"
    monkeypatch.setattr(ut_check, "_MAIN_UT_BASELINE_FILE", baseline_file)
    monkeypatch.setenv("MAIN2MAIN_BASE_UT_BASELINE", "0")
    assert compute_base_ut_baseline(repo, vllm) is None
    assert not baseline_file.exists()


def test_compute_base_ut_baseline_no_checkout_skips(tmp_path, monkeypatch):
    repo = tmp_path / "ascend"
    repo.mkdir()
    baseline_file = tmp_path / "base_ut_failures.json"
    monkeypatch.setattr(ut_check, "_MAIN_UT_BASELINE_FILE", baseline_file)
    assert compute_base_ut_baseline(repo, tmp_path / "missing") is None
    assert not baseline_file.exists()


def test_compute_base_ut_baseline_snapshot_failure_writes_nothing(
        tmp_path, monkeypatch):
    repo = tmp_path / "ascend"
    repo.mkdir()
    vllm = tmp_path / "vllm"
    (vllm / ".git").mkdir(parents=True)
    baseline_file = tmp_path / "base_ut_failures.json"
    monkeypatch.setattr(ut_check, "_MAIN_UT_BASELINE_FILE", baseline_file)
    monkeypatch.setattr(ut_check, "_collect_cpu_ut_files",
                        lambda r: ["tests/ut/test_a.py"])
    monkeypatch.setattr(ut_check, "_ensure_ut_venv",
                        lambda spec: (None, ""))  # no venv, no pytest
    monkeypatch.setattr(ut_check.shutil, "which", lambda name: None)
    assert compute_base_ut_baseline(repo, vllm) is None
    assert not baseline_file.exists()


def test_failure_excerpt_anchors_to_own_section():
    """Two tests raising the same error must not share an excerpt.

    clean.find(needle) returns the FIRST occurrence of the error text in
    the whole run — run 38405850804's request_lifecycle violations
    carried a build_cache test's traceback, sending the root-cause
    analysis to the wrong file.  The excerpt must anchor at the failing
    test's own `_____ name _____` section header.
    """
    early_err = "FileNotFoundError: [Errno 2] /tmp/zz_dead_dir/vllm_ascend_1.log"
    clean = (
        "_________________________ test_build_cache_env _________________________\n"
        f"E   {early_err}\n"
        "E   assert csrc default\n"
        "\n"
        "_________________________ test_request_lifecycle ________________________\n"
        f"E   {early_err}\n"
        "E   assert emit re-opened the dead stream\n"
    )
    line = ("FAILED tests/ut/x/test_request_lifecycle.py::test_request_lifecycle"
            f" - {early_err}")

    ex = ut_check._failure_excerpt(clean, line)

    assert "test_request_lifecycle" in ex
    assert "dead stream" in ex
    assert "csrc default" not in ex


def test_failure_excerpt_falls_back_to_first_occurrence():
    """Without a matching section header, the excerpt degrades to the
    first needle occurrence instead of returning nothing."""
    err = "ValueError: bad config"
    clean = (
        f"E   {err}\n"
        "E   more detail\n"
    )
    line = f"FAILED tests/ut/x/test_cfg.py::test_cfg - {err}"

    ex = ut_check._failure_excerpt(clean, line)

    assert "bad config" in ex


def _make_built_ascend_tree(tmp_path):
    """A minimal built ascend tree: vllm_ascend package with an a3 profile."""
    repo = tmp_path / "ascend"
    pkg = repo / "vllm_ascend"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "device_config.py").write_text("VALUE = 'real-tree'\n",
                                          encoding="utf-8")
    (pkg / "_build_info.py").write_text(
        "# Auto-generated file\n__device_type__ = 'A3'\n", encoding="utf-8")
    return repo


def test_pure_cpu_swap_rewrites_device_type_in_place(tmp_path):
    repo = _make_built_ascend_tree(tmp_path)

    info, original = ut_check._swap_pure_cpu_build_info(repo)

    assert info is not None and original is not None
    # Upstream's SOC-less cpu-ut build writes exactly this device family;
    # the rest of the generated file must survive byte-for-byte.
    swapped = info.read_text()
    assert "__device_type__ = 'A2'" in swapped
    assert "# Auto-generated file" in swapped
    assert "__device_type__ = 'A3'" not in swapped
    assert swapped.replace("A2", "A3") == original


def test_pure_cpu_swap_restore_round_trip(tmp_path):
    repo = _make_built_ascend_tree(tmp_path)

    info, original = ut_check._swap_pure_cpu_build_info(repo)
    ut_check._restore_build_info(info, original)

    assert info.read_text() == \
        "# Auto-generated file\n__device_type__ = 'A3'\n"


def test_pure_cpu_swap_double_quoted_value(tmp_path):
    """Installs that quote with double quotes swap just as cleanly."""
    repo = _make_built_ascend_tree(tmp_path)
    info = repo / "vllm_ascend" / "_build_info.py"
    info.write_text('__device_type__ = "A3"\n', encoding="utf-8")

    swapped, original = ut_check._swap_pure_cpu_build_info(repo)

    assert swapped is not None and original is not None
    assert "__device_type__ = 'A2'" in swapped.read_text()
    ut_check._restore_build_info(swapped, original)
    assert swapped.read_text() == '__device_type__ = "A3"\n'


def test_pure_cpu_swap_env_override_disables(tmp_path, monkeypatch):
    repo = _make_built_ascend_tree(tmp_path)
    monkeypatch.setenv("MAIN2MAIN_UT_PURE_CPU", "0")

    assert ut_check._swap_pure_cpu_build_info(repo) == (None, None)
    assert "__device_type__ = 'A3'" in \
        (repo / "vllm_ascend" / "_build_info.py").read_text()


def test_pure_cpu_swap_missing_or_malformed_is_noop(tmp_path, capsys):
    missing = tmp_path / "ascend"
    missing.mkdir()

    assert ut_check._swap_pure_cpu_build_info(missing) == (None, None)

    malformed = tmp_path / "ascend2"
    pkg = malformed / "vllm_ascend"
    pkg.mkdir(parents=True)
    (pkg / "_build_info.py").write_text("# no device type here\n",
                                        encoding="utf-8")

    assert ut_check._swap_pure_cpu_build_info(malformed) == (None, None)
    assert (pkg / "_build_info.py").read_text() == "# no device type here\n"
    # The skip is loud, not silent — an operator must be able to see why
    # the batch graded the tree's own build profile.
    assert "no __device_type__ line" in capsys.readouterr().out


def test_restore_build_info_noop_without_swap(capsys):
    ut_check._restore_build_info(None, None)

    assert "restored" not in capsys.readouterr().out


def test_build_ut_env_paths_and_safepath(tmp_path):
    repo = _make_built_ascend_tree(tmp_path)
    vllm = tmp_path / "vllm"
    vllm.mkdir()
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()

    env = ut_check._build_ut_env(repo, vllm, fake_bin)

    parts = env["PYTHONPATH"].split(":")
    assert parts[0] == str(repo.resolve())
    assert parts[1] == str(vllm.resolve())
    # `python -m pytest` prepends the graded tree (cwd) ahead of
    # PYTHONPATH; PYTHONSAFEPATH gives -m the console-script sys.path
    # semantics so the invocation matches upstream CI on both paths
    # (run 38421012498).
    assert env["PYTHONSAFEPATH"] == "1"


def test_check_ut_uses_venv_pytest_script(tmp_path, monkeypatch):
    """Upstream CI invokes the pytest console script.  `python -m pytest`
    instead prepends the repo cwd to sys.path AHEAD of PYTHONPATH — the
    batch prefers the console-script form and sets PYTHONSAFEPATH for the
    -m fallback (invocation parity with upstream CI)."""
    repo = tmp_path / "ascend"
    repo.mkdir()
    vllm = tmp_path / "vllm"
    vllm.mkdir()
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    py.write_text("")
    script = venv / "bin" / "pytest"
    script.write_text("")

    monkeypatch.setattr(ut_check, "_collect_cpu_ut_files",
                        lambda repo: ["tests/ut/test_a.py"])
    monkeypatch.setattr(ut_check, "_ensure_ut_venv",
                        lambda spec: (venv, str(py)))
    ok = SimpleNamespace(returncode=0, stdout="1 passed in 0.01s\n",
                         stderr="")
    seen_cmds = []

    def _fake_run(cmd, **kwargs):
        seen_cmds.append((cmd, kwargs))
        return ok

    monkeypatch.setattr(ut_check.subprocess, "run", _fake_run)

    result = check_ut(repo, vllm_path=vllm)

    cmd = seen_cmds[0][0]
    assert cmd[0] == str(script)
    assert "main2main_flow.scripts.utils.ut_namespace" in cmd
    assert result["violations"] == []
