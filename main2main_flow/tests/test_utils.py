"""run_format_sh environment contract (a3-16 branch)."""
from __future__ import annotations

import platform
import subprocess
from pathlib import Path

from main2main_flow.scripts.utils.utils import run_format_sh


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
