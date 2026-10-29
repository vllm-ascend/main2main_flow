"""run_format_sh environment contract (a3-16 branch)."""
from __future__ import annotations

import os
import platform
import subprocess
from pathlib import Path

from main2main_flow.scripts.utils import utils as utils_mod
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

    def fake_stream(cmd, *, cwd, env, timeout=None):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        captured["env"] = env
        captured["timeout"] = timeout
        # gitleaks.sh downloads the binary into the repo root mid-run.
        (repo / "gitleaks").write_bytes(b"binary")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(utils_mod, "_run_streaming", fake_stream)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("MAIN2MAIN_FORMAT_TIMEOUT", raising=False)

    run_format_sh(repo)

    assert captured["cmd"] == ["bash", str(repo / "format.sh"), "ci"]
    assert captured["cwd"] == str(repo)
    assert captured["timeout"] == 1800
    env = captured["env"]
    # Both skipped hooks are environment-only problems: gitleaks cannot run
    # in the a3 image, and actionlint's shellcheck/pyflakes subprocesses hang
    # (PR #20156 committed that suppression into .pre-commit-config.yaml;
    # the guard reverts it, so it must live here).
    assert env["SKIP"] == "gitleaks-offline-scan,actionlint"
    assert env["PYTHONUNBUFFERED"] == "1"
    assert env["PRE_COMMIT_HOME"] == str(
        tmp_path / ".cache"
        / f"main2main-pre-commit-a3-16-{platform.machine()}")
    # The downloaded binary must not survive the run (would pollute the diff).
    assert not (repo / "gitleaks").exists()


def test_echo_pipe_streams_cleaned_lines(monkeypatch) -> None:
    printed: list[str] = []
    monkeypatch.setattr(utils_mod, "ts_print",
                        lambda text, **kwargs: printed.append(text))

    class FakePipe:
        def __init__(self, lines: list[str]) -> None:
            self._lines = iter(lines)
            self.closed = False

        def readline(self) -> str:
            return next(self._lines, "")

        def close(self) -> None:
            self.closed = True

    pipe = FakePipe(["\x1b[32mruff check\x1b[0m\n", "Passed\n"])
    chunks: list[str] = []
    utils_mod._echo_pipe(pipe, chunks, "[stderr] ")

    # Buffers stay raw (violation extraction needs the exact text), the log
    # gets ANSI-stripped lines with the stream prefix.
    assert chunks == ["\x1b[32mruff check\x1b[0m\n", "Passed\n"]
    assert printed == ["[stderr] ruff check", "[stderr] Passed"]
    assert pipe.closed


def test_run_streaming_captures_both_pipes(tmp_path: Path) -> None:
    script = ("printf 'first\\n'; printf 'oops\\n' >&2; printf 'second\\n'")
    r = utils_mod._run_streaming(
        ["bash", "-c", script], cwd=str(tmp_path), env=os.environ.copy())

    assert r.returncode == 0
    assert r.stdout == "first\nsecond\n"
    assert r.stderr == "oops\n"


def test_run_streaming_timeout_kills_process_group(tmp_path: Path) -> None:
    """A hung pre-commit must be killed, not waited on forever.

    Five nightly runs hung 6-17 h inside format.sh (10-19..10-23); the
    timeout is the only thing that turns that into a visible failure.
    """
    import time

    start = time.monotonic()
    r = utils_mod._run_streaming(
        ["bash", "-c", "sleep 30"], cwd=str(tmp_path),
        env=os.environ.copy(), timeout=0.3)
    elapsed = time.monotonic() - start

    assert r.returncode == utils_mod.FORMAT_TIMEOUT_RC
    assert "timed out after 0.3s" in r.stderr
    assert elapsed < 10
