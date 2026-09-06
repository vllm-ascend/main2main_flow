"""ut_verify CLI — the adapter's verify-loop entry point."""
from __future__ import annotations

from types import SimpleNamespace

from main2main_flow.scripts.utils import ut_check, ut_verify
from main2main_flow.scripts.utils.ut_verify import main


def test_resolve_pytest_cmd_prefers_explicit_python(tmp_path):
    py = tmp_path / "python"
    py.write_text("")
    assert ut_verify._resolve_pytest_cmd(str(py)) == [str(py), "-m", "pytest"]


def test_resolve_pytest_cmd_falls_back_to_system(monkeypatch, tmp_path):
    monkeypatch.setattr(ut_verify, "_ut_base_dir", lambda: tmp_path / "nope")
    cmd = ut_verify._resolve_pytest_cmd("")
    assert len(cmd) == 1 and "pytest" in cmd[0]  # system pytest fallback


def test_main_rejects_missing_test_file(tmp_path, capsys):
    rc = main(["--repo", str(tmp_path), "--vllm", str(tmp_path),
               "tests/ut/test_missing.py"])
    assert rc == 2
    assert "not under" in capsys.readouterr().err


def test_main_runs_pytest_writes_log_and_returns_exit(
        tmp_path, monkeypatch):
    repo = tmp_path / "ascend"
    (repo / "tests" / "ut").mkdir(parents=True)
    (repo / "tests" / "ut" / "test_x.py").write_text("def test_x():\n"
                                                     "    assert 1\n")
    vllm = tmp_path / "vllm"
    vllm.mkdir()
    venv_py = tmp_path / "py"
    venv_py.write_text("")
    log_dir = tmp_path / "utbase"
    monkeypatch.setattr(ut_check, "_ut_base_dir", lambda: log_dir)
    monkeypatch.setattr(ut_verify, "_ut_base_dir", lambda: log_dir)
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    monkeypatch.setattr(ut_verify, "_make_fake_npu_smi", lambda: fake_bin)

    ok = SimpleNamespace(returncode=3, stdout="1 failed\n", stderr="")
    seen = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = cmd
        seen["kwargs"] = kwargs
        return ok

    monkeypatch.setattr(ut_verify.subprocess, "run", _fake_run)
    rc = main(["--repo", str(repo), "--vllm", str(vllm),
               "--python", str(venv_py),
               "tests/ut/test_x.py"])
    assert rc == 3
    cmd = seen["cmd"]
    assert cmd[:3] == [str(tmp_path / "py"), "-m", "pytest"]
    assert "--tb=long" in cmd
    assert "main2main_flow.scripts.utils.ut_namespace" in cmd
    assert "tests/ut/test_x.py" in cmd
    env = seen["kwargs"]["env"]
    assert env["HF_HUB_OFFLINE"] == "1"
    assert seen["kwargs"]["cwd"] == str(repo)
    log_text = (log_dir / "ut_verify_last.log").read_text()
    assert "1 failed" in log_text
    assert log_text.startswith("# ut_verify full log")


def test_main_timeout_returns_124(tmp_path, monkeypatch):
    repo = tmp_path / "ascend"
    (repo / "tests" / "ut").mkdir(parents=True)
    (repo / "tests" / "ut" / "test_x.py").write_text("")
    log_dir = tmp_path / "utbase"
    monkeypatch.setattr(ut_check, "_ut_base_dir", lambda: log_dir)
    monkeypatch.setattr(ut_verify, "_ut_base_dir", lambda: log_dir)
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    monkeypatch.setattr(ut_verify, "_make_fake_npu_smi", lambda: fake_bin)
    monkeypatch.setattr(ut_verify.subprocess, "run",
                        lambda *a, **k: (_ for _ in ()).throw(
                            ut_verify.subprocess.TimeoutExpired(cmd="pytest",
                                                                timeout=1)))
    rc = main(["--repo", str(repo), "--vllm", str(repo),
               "tests/ut/test_x.py"])
    assert rc == 124
