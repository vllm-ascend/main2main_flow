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
    assert env["PYTHONPATH"].endswith(_flow_root())
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["ASCEND_RT_VISIBLE_DEVICES"] == ""
    assert "CUDA_VISIBLE_DEVICES" not in env
    assert env["TORCH_DEVICE_BACKEND_AUTOLOAD"] == "0"
    assert env["VLLM_WORKER_MULTIPROC_METHOD"] == "spawn"
    assert env["PATH"].startswith(str(fake_bin))


def test_ensure_ut_venv_reuses_matching_marker(tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    py = venv / "bin" / "python"
    py.write_text("")
    (venv / "m2m_meta.json").write_text(json.dumps({"numpy_spec": "==1.26.4"}))

    def _boom(*a, **k):
        raise AssertionError("venv must be reused, not recreated")

    monkeypatch.setattr(ut_check.subprocess, "run", _boom)
    monkeypatch.setenv("MAIN2MAIN_UT_VENV", str(venv))
    got_dir, got_py = _ensure_ut_venv("==1.26.4")
    assert got_dir == venv
    assert got_py == str(py)


def test_ensure_ut_venv_recreates_on_spec_mismatch(tmp_path, monkeypatch):
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python").write_text("")
    (venv / "m2m_meta.json").write_text(json.dumps({"numpy_spec": "==1.24.0"}))

    ok = SimpleNamespace(returncode=0, stdout="", stderr="")
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
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
