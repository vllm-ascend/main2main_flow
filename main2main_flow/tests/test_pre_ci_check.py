"""Dual-lane pre-CI gate contract (main2main_flow).

The flow validates mypy against the main vllm tree PLUS the pinned
release-tag worktree (both at every step's pre-CI via run_check and at
the final quality gate), and checks imported symbols against the release
tree.  These tests pin that contract: the release-tree parameters must
stay in the signatures, run_check must thread them into the checks it
runs, and a missing release worktree must be recorded as a skipped
release_lane check — never silently absent.
"""
from __future__ import annotations

import inspect
import subprocess
from pathlib import Path


from main2main_flow.scripts.utils import pre_ci_check
from main2main_flow.scripts.utils.final_quality_gate import run_final_quality_gate
from main2main_flow.scripts.utils.ut_check import check_ut


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True)
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=str(repo), check=True,
                       capture_output=True, text=True)
    git("init", "-q")
    git("checkout", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    (repo / "vllm_ascend").mkdir()
    (repo / "vllm_ascend" / "code.py").write_text("x = 1\n", encoding="utf-8")
    git("add", "-A")
    git("commit", "-q", "-m", "base")


def test_check_ut_dual_version_signature() -> None:
    # The release-lane UT batch is a final-gate-only feature; per-step
    # pre_ci UT stays main-only (flow never passes the release args there).
    params = inspect.signature(check_ut).parameters
    assert "vllm_release_path" in params
    assert "release_tag" in params
    assert "vllm_path" in params


def test_check_mypy_dual_version_signature() -> None:
    params = inspect.signature(pre_ci_check._check_mypy).parameters
    assert "vllm_release_path" in params
    assert params["vllm_release_path"].default is None


def test_final_quality_gate_release_signature() -> None:
    params = inspect.signature(run_final_quality_gate).parameters
    assert "vllm_release_path" in params
    assert "release_tag" in params


def test_run_check_without_vllm_path_skips_mypy_ut(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)
    called = []
    monkeypatch.setattr(pre_ci_check, "_check_mypy",
                        lambda *a, **k: called.append("mypy") or {"violations": []})
    monkeypatch.setattr(pre_ci_check, "_check_ut",
                        lambda *a, **k: called.append("ut") or {"violations": []})
    result = pre_ci_check.run_check(repo, "v0.27.1")
    assert result["all_passed"] is True
    assert called == []
    assert [c["name"] for c in result["checks"]] == [
        "version_strings", "temp_files", "format", "release_lane"]


def test_run_check_runs_mypy_ut_with_vllm_path(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)
    monkeypatch.setattr(pre_ci_check, "_check_mypy",
                        lambda *a, **k: {
                            "violations": ["a.py:1: error: boom"],
                            "detail": "1 mypy issue(s)"})
    monkeypatch.setattr(pre_ci_check, "_check_ut",
                        lambda *a, **k: {
                            "violations": [], "detail": "UT clean"})
    result = pre_ci_check.run_check(repo, "v0.27.1", vllm_path=tmp_path / "vllm")
    assert result["all_passed"] is False
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["mypy"]["passed"] is False
    assert by_name["ut"]["passed"] is True
    assert "skipped" not in by_name["ut"] or by_name["ut"]["skipped"] is False


def test_run_check_mypy_ut_skipped_never_fails(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)
    monkeypatch.setattr(pre_ci_check, "_check_mypy",
                        lambda *a, **k: {"violations": [], "detail": "",
                                         "skipped": True})
    monkeypatch.setattr(pre_ci_check, "_check_ut",
                        lambda *a, **k: {"violations": [], "detail": "",
                                         "skipped": True})
    result = pre_ci_check.run_check(repo, "v0.27.1", vllm_path=tmp_path / "vllm")
    assert result["all_passed"] is True


def test_run_check_release_path_threaded_to_mypy_and_imports(
        monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)
    seen: dict[str, tuple] = {}

    def fake_mypy(repo_arg, vllm_arg, release_arg=None):
        seen["mypy"] = (vllm_arg, release_arg)
        return {"violations": [], "detail": "mypy clean"}

    def fake_imports(repo_arg, vllm_arg, release_arg=None):
        seen["imports"] = (vllm_arg, release_arg)
        return {"violations": []}

    monkeypatch.setattr(pre_ci_check, "_check_mypy", fake_mypy)
    monkeypatch.setattr(pre_ci_check, "_check_broken_imports", fake_imports)
    monkeypatch.setattr(pre_ci_check, "_check_ut",
                        lambda *a, **k: {"violations": [], "detail": ""})
    release = tmp_path / "vllm-release"
    release.mkdir()
    result = pre_ci_check.run_check(repo, "v0.28.0",
                                    vllm_path=tmp_path / "vllm",
                                    vllm_release_path=release)
    assert result["all_passed"] is True
    # vllm_path and vllm_release_path are Paths — compare resolved.
    assert seen["mypy"][1] == release
    assert seen["imports"][1] == release


def test_run_check_no_release_path_emits_skipped_release_lane(
        monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)
    monkeypatch.setattr(pre_ci_check, "_check_mypy",
                        lambda *a, **k: {"violations": [], "detail": ""})
    monkeypatch.setattr(pre_ci_check, "_check_ut",
                        lambda *a, **k: {"violations": [], "detail": ""})
    result = pre_ci_check.run_check(repo, "v0.28.0", vllm_path=tmp_path / "vllm")
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["release_lane"]["skipped"] is True
    assert by_name["release_lane"]["passed"] is True
    assert result["all_passed"] is True


def test_run_check_with_release_path_has_no_pseudo_check(
        monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)
    monkeypatch.setattr(pre_ci_check, "_check_mypy",
                        lambda *a, **k: {"violations": [], "detail": ""})
    monkeypatch.setattr(pre_ci_check, "_check_ut",
                        lambda *a, **k: {"violations": [], "detail": ""})
    release = tmp_path / "vllm-release"
    release.mkdir()
    result = pre_ci_check.run_check(repo, "v0.28.0",
                                    vllm_path=tmp_path / "vllm",
                                    vllm_release_path=release)
    assert "release_lane" not in {c["name"] for c in result["checks"]}


def test_run_check_release_mypy_failure_fails_step(monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)
    monkeypatch.setattr(pre_ci_check, "_check_mypy",
                        lambda *a, **k: {
                            "violations": ['a.py:1: error: Missing positional '
                                           'argument "max_seq_len_np" in call '
                                           'to "AscendInputBatch"  [call-arg]'],
                            "detail": "1 mypy issue(s) (release(vllm-release)/3.10)"})
    monkeypatch.setattr(pre_ci_check, "_check_ut",
                        lambda *a, **k: {"violations": [], "detail": ""})
    release = tmp_path / "vllm-release"
    release.mkdir()
    result = pre_ci_check.run_check(repo, "v0.28.0",
                                    vllm_path=tmp_path / "vllm",
                                    vllm_release_path=release)
    assert result["all_passed"] is False
    mypy_check = next(c for c in result["checks"] if c["name"] == "mypy")
    assert any("release" in v or "max_seq_len_np" in v
               for v in mypy_check["violations"])


def test_run_check_runs_mypy_ut_concurrently(monkeypatch, tmp_path: Path) -> None:
    # mypy and UT are submitted to a 2-worker pool: a 2-party barrier inside
    # both fakes passes only if they genuinely overlap — sequential execution
    # would hang until the barrier breaks (BrokenBarrierError).
    import threading

    repo = tmp_path / "ascend"
    _init_repo(repo)
    barrier = threading.Barrier(2, timeout=10)

    def fake_mypy(*a, **k):
        barrier.wait()
        return {"violations": [], "detail": "mypy ok"}

    def fake_ut(*a, **k):
        barrier.wait()
        return {"violations": [], "detail": "ut ok"}

    monkeypatch.setattr(pre_ci_check, "_check_mypy", fake_mypy)
    monkeypatch.setattr(pre_ci_check, "_check_ut", fake_ut)
    result = pre_ci_check.run_check(repo, "v0.27.1", vllm_path=tmp_path / "vllm")
    assert result["all_passed"] is True
    by_name = {c["name"]: c for c in result["checks"]}
    assert by_name["mypy"]["detail"] == "mypy ok"
    assert by_name["ut"]["detail"] == "ut ok"


# --- persistent mypy lint venv (_ensure_mypy_venv) -------------------------
# Replaces the per-call temp venv (mkdtemp + rmtree): the lint venv is built
# once under MAIN2MAIN_MYPY_VENV (default workspace/mypy_venv) and reused
# while the marker's numpy_spec matches triton-ascend's constraint; every
# failure path falls back to the system mypy ((None, "")).
SPEC = ">=1.26.4,<2.1"


def _make_venv(base: Path, spec: str = SPEC) -> None:
    (base / "bin").mkdir(parents=True)
    (base / "bin" / "python").write_text("", encoding="utf-8")
    import json
    (base / "m2m_meta.json").write_text(json.dumps({"numpy_spec": spec}),
                                        encoding="utf-8")


def _ok_run(monkeypatch, calls):
    from types import SimpleNamespace

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pre_ci_check.subprocess, "run", fake_run)


def _ok_pip(monkeypatch, calls):
    def fake_pip(python, args):
        calls.append(args)
        from types import SimpleNamespace
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pre_ci_check, "pip_install_with_fallback", fake_pip)


def test_mypy_venv_reused_when_marker_matches(monkeypatch, tmp_path):
    base = tmp_path / "mypy_venv"
    _make_venv(base)
    monkeypatch.setenv("MAIN2MAIN_MYPY_VENV", str(base))

    def no_run(*a, **k):  # reuse must not shell out at all
        raise AssertionError("venv creation on the reuse path")

    monkeypatch.setattr(pre_ci_check.subprocess, "run", no_run)
    venv_dir, venv_python = pre_ci_check._ensure_mypy_venv(SPEC)
    assert venv_dir == base
    assert venv_python == str(base / "bin" / "python")


def test_mypy_venv_rebuilt_on_spec_mismatch(monkeypatch, tmp_path):
    base = tmp_path / "mypy_venv"
    _make_venv(base, spec=">=2.0")  # stale spec
    monkeypatch.setenv("MAIN2MAIN_MYPY_VENV", str(base))
    run_calls, pip_calls = [], []
    _ok_run(monkeypatch, run_calls)
    _ok_pip(monkeypatch, pip_calls)

    venv_dir, venv_python = pre_ci_check._ensure_mypy_venv(SPEC)
    assert venv_dir == base and venv_python == str(base / "bin" / "python")
    # old tree removed, venv re-created, numpy pinned, marker rewritten
    assert any("venv" in c for c in run_calls)
    assert any(f"numpy{SPEC}" in a for a in pip_calls)
    import json
    assert json.loads((base / "m2m_meta.json").read_text())["numpy_spec"] == SPEC


def test_mypy_venv_unreadable_marker_recreates(monkeypatch, tmp_path):
    base = tmp_path / "mypy_venv"
    _make_venv(base)
    (base / "m2m_meta.json").write_text(":::: [broken", encoding="utf-8")
    monkeypatch.setenv("MAIN2MAIN_MYPY_VENV", str(base))
    run_calls, pip_calls = [], []
    _ok_run(monkeypatch, run_calls)
    _ok_pip(monkeypatch, pip_calls)
    venv_dir, _ = pre_ci_check._ensure_mypy_venv(SPEC)
    assert venv_dir == base
    assert run_calls  # went through creation, not reuse


def test_mypy_venv_falls_back_when_creation_fails(monkeypatch, tmp_path):
    base = tmp_path / "mypy_venv"
    monkeypatch.setenv("MAIN2MAIN_MYPY_VENV", str(base))
    from types import SimpleNamespace

    monkeypatch.setattr(
        pre_ci_check.subprocess, "run",
        lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="boom"))
    assert pre_ci_check._ensure_mypy_venv(SPEC) == (None, "")
    # no marker: a failed creation must not look reusable next call
    assert not (base / "m2m_meta.json").exists()


def test_mypy_venv_falls_back_when_numpy_install_fails(monkeypatch, tmp_path):
    base = tmp_path / "mypy_venv"
    monkeypatch.setenv("MAIN2MAIN_MYPY_VENV", str(base))
    from types import SimpleNamespace
    _ok_run(monkeypatch, [])

    def bad_pip(python, args):
        return SimpleNamespace(returncode=1, stdout="", stderr="no wheel")

    monkeypatch.setattr(pre_ci_check, "pip_install_with_fallback", bad_pip)
    # a broken numpy silently leaves system numpy 2.x -> treat as no venv
    assert pre_ci_check._ensure_mypy_venv(SPEC) == (None, "")
    assert not (base / "m2m_meta.json").exists()


def test_mypy_venv_falls_back_on_timeout(monkeypatch, tmp_path):
    base = tmp_path / "mypy_venv"
    monkeypatch.setenv("MAIN2MAIN_MYPY_VENV", str(base))

    def slow(*a, **k):
        raise pre_ci_check.subprocess.TimeoutExpired(cmd="venv", timeout=180)

    monkeypatch.setattr(pre_ci_check.subprocess, "run", slow)
    assert pre_ci_check._ensure_mypy_venv(SPEC) == (None, "")


def test_mypy_venv_no_temp_dir_residue(monkeypatch, tmp_path):
    # The old implementation mkdtemp'ed into the system temp every call and
    # destroyed it in a finally; the persistent venv must never touch it.
    import tempfile

    base = tmp_path / "mypy_venv"
    monkeypatch.setenv("MAIN2MAIN_MYPY_VENV", str(base))

    def no_mkdtemp(*a, **k):
        raise AssertionError("temp venv created in system temp")

    monkeypatch.setattr(tempfile, "mkdtemp", no_mkdtemp)
    # fallback path (creation fails) — and the success path below
    monkeypatch.setattr(
        pre_ci_check.subprocess, "run",
        lambda *a, **k: (_ for _ in ()).throw(
            pre_ci_check.subprocess.TimeoutExpired(cmd="venv", timeout=180)))
    assert pre_ci_check._ensure_mypy_venv(SPEC) == (None, "")

    run_calls, pip_calls = [], []
    _ok_run(monkeypatch, run_calls)
    _ok_pip(monkeypatch, pip_calls)
    venv_dir, _ = pre_ci_check._ensure_mypy_venv(SPEC)
    assert venv_dir == base  # success path also stays under MAIN2MAIN_MYPY_VENV
