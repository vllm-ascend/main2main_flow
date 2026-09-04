"""run_format_sh environment contract (a3-16 branch)."""
from __future__ import annotations

import platform
import subprocess
from pathlib import Path

from main2main_flow.scripts.utils.utils import (pip_install_with_fallback,
                                                run_format_sh)


def test_pip_install_with_fallback_uses_second_index_on_failure(
        monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        code = 0 if len(calls) >= 2 else 1
        return subprocess.CompletedProcess(cmd, code, "", "403")

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = pip_install_with_fallback(Path("/venv/bin/python"), ["-q", "numpy==1.26.4"])
    assert r.returncode == 0
    assert len(calls) == 2
    assert calls[1][-2:] == ["-i", "https://mirrors.aliyun.com/pypi/simple/"]


def test_pip_install_with_fallback_no_retry_on_success(monkeypatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    r = pip_install_with_fallback(Path("/venv/bin/python"), ["numpy==1.26.4"])
    assert r.returncode == 0
    assert len(calls) == 1


def test_run_format_sh_env_and_gitleaks_cleanup(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "ascend"
    repo.mkdir()
    (repo / "format.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs["env"]
        # gitleaks.sh downloads the binary into the repo root mid-run.
        (repo / "gitleaks").write_bytes(b"binary")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    run_format_sh(repo)

    assert captured["cmd"] == ["bash", str(repo / "format.sh"), "ci"]
    env = captured["env"]
    assert env["SKIP"] == "gitleaks-offline-scan"
    assert env["PRE_COMMIT_HOME"] == str(
        tmp_path / ".cache"
        / f"main2main-pre-commit-a3-16-{platform.machine()}")
    # The downloaded binary must not survive the run (would pollute the diff).
    assert not (repo / "gitleaks").exists()
