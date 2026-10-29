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
import json
import subprocess
from pathlib import Path


from main2main_flow.scripts.utils import final_quality_gate as fqg
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
        "version_strings", "temp_files", "format", "maintainability",
        "release_lane"]


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
# failure path falls back to the system mypy ((None, "")).  Reuse also
# probes the ACTUAL importable numpy — a marker-healthy venv whose numpy
# lives in system site-packages drifts when e2e's vllm install upgrades
# system numpy (run 38220121641: 1.26.4 -> 2.3.5) — and drift rebuilds.
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
        # the numpy health probe (reuse + post-install) must report a
        # healthy pin, or the venv is treated as drifted
        if "import numpy" in str(cmd[-1]):
            return SimpleNamespace(returncode=0, stdout="1.26.4\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pre_ci_check.subprocess, "run", fake_run)


def _probe_run(monkeypatch, calls, versions=("1.26.4",)):
    """subprocess fake answering the numpy health probes in sequence.

    Each "import numpy" probe consumes the next version (the last one
    repeats); None = probe fails (rc=1).  Non-probe calls (venv creation)
    return the plain ok shape.
    """
    from types import SimpleNamespace
    seq = list(versions)

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if "import numpy" in str(cmd[-1]):
            v = seq.pop(0) if len(seq) > 1 else seq[0]
            if v is None:
                return SimpleNamespace(returncode=1, stdout="", stderr="boom")
            return SimpleNamespace(returncode=0, stdout=f"{v}\n", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(pre_ci_check.subprocess, "run", fake_run)


def _venv_created(calls) -> bool:
    # venv creation is [sys.executable, "-m", "venv", <path>, "--system-site-packages"]
    return any("venv" in c and "-m" in c for c in calls)


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
    calls = []
    _probe_run(monkeypatch, calls)  # reuse probes numpy, must NOT create

    venv_dir, venv_python = pre_ci_check._ensure_mypy_venv(SPEC)
    assert venv_dir == base
    assert venv_python == str(base / "bin" / "python")
    assert calls and not _venv_created(calls)


def test_mypy_venv_rebuilt_when_numpy_drifted(monkeypatch, tmp_path):
    # Run 38220121641: e2e's vllm install upgraded system numpy under a
    # --system-site-packages venv that never owned its own copy — the
    # marker stayed healthy while `import numpy` flipped 1.26.4 -> 2.3.5.
    # Reuse must detect the drift and rebuild instead of linting on 2.x.
    base = tmp_path / "mypy_venv"
    _make_venv(base)
    monkeypatch.setenv("MAIN2MAIN_MYPY_VENV", str(base))
    run_calls, pip_calls = [], []
    _probe_run(monkeypatch, run_calls, versions=["2.3.5", "1.26.4"])
    _ok_pip(monkeypatch, pip_calls)

    venv_dir, _ = pre_ci_check._ensure_mypy_venv(SPEC)
    assert venv_dir == base
    assert _venv_created(run_calls)
    # the rebuild forces the wheel into the venv (immune to system flips)
    assert any("--ignore-installed" in a for a in pip_calls)
    import json
    assert json.loads((base / "m2m_meta.json").read_text())["numpy_spec"] == SPEC


def test_mypy_venv_rebuilt_when_numpy_unimportable(monkeypatch, tmp_path):
    base = tmp_path / "mypy_venv"
    _make_venv(base)
    monkeypatch.setenv("MAIN2MAIN_MYPY_VENV", str(base))
    run_calls, pip_calls = [], []
    _probe_run(monkeypatch, run_calls, versions=[None, "1.26.4"])
    _ok_pip(monkeypatch, pip_calls)

    venv_dir, venv_python = pre_ci_check._ensure_mypy_venv(SPEC)
    assert venv_dir == base
    assert venv_python == str(base / "bin" / "python")
    assert _venv_created(run_calls)


def test_numpy_drifted_predicate():
    from main2main_flow.scripts.utils.ut_check import _numpy_drifted
    assert not _numpy_drifted("==1.26.4", "1.26.4")
    assert _numpy_drifted("==1.26.4", "2.3.5")    # run 38220121641
    assert _numpy_drifted("==1.26.4", "")         # unimportable counts
    # a 2.x the spec admits is NOT drift: rebuilding would reinstall the
    # same thing (infinite churn); _check_mypy warns about it instead
    assert not _numpy_drifted(">=1.26", "2.3.5")
    # empty spec = no pin to violate
    assert not _numpy_drifted("", "1.26.4")


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


# --- mypy cache isolation (PR 19089 false green) ---------------------------
#
# mypy 1.11.1 reuses green cache entries after a mypy.ini per-module
# override is removed and after the imported module disappears from the
# PYTHONPATH tree.  The persistent cache must be bucketed by mypy.ini +
# vllm HEAD (+ numpy), and the push-time gate must not use it at all.

def test_mypy_cache_key_buckets_ini_head_and_numpy(tmp_path: Path) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)
    (repo / "mypy.ini").write_text("[mypy]\nstrict = True\n", encoding="utf-8")
    vllm = tmp_path / "vllm"
    _init_repo(vllm)

    spec = ">=1.26.4,<2.1"
    k1 = pre_ci_check._mypy_cache_key(repo, vllm, spec)
    assert k1 and len(k1) == 12
    assert pre_ci_check._mypy_cache_key(repo, vllm, spec) == k1

    # A discarded round's mypy.ini override must not share the bucket.
    (repo / "mypy.ini").write_text(
        "[mypy-vllm.transformers_utils.configs.glm5_next]\n"
        "ignore_missing_imports = True\n", encoding="utf-8")
    k2 = pre_ci_check._mypy_cache_key(repo, vllm, spec)
    assert k2 and k2 != k1

    # A different vllm tree (main advanced) gets its own bucket.
    (vllm / "vllm_ascend" / "code.py").write_text("x = 2\n", encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "next"], cwd=str(vllm),
                   check=True, capture_output=True, text=True)
    k3 = pre_ci_check._mypy_cache_key(repo, vllm, spec)
    assert k3 and k3 not in (k1, k2)

    # Installed numpy types leak into results: the constraint is keyed too.
    assert pre_ci_check._mypy_cache_key(repo, vllm, ">=2.0") != k3


def test_mypy_cache_key_none_when_inputs_unreadable(tmp_path: Path) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)  # no mypy.ini -> must run --no-incremental
    vllm = tmp_path / "vllm"
    _init_repo(vllm)
    assert pre_ci_check._mypy_cache_key(repo, vllm, "") is None
    assert pre_ci_check._mypy_cache_key(repo, None, "") is None
    (repo / "mypy.ini").write_text("[mypy]\n", encoding="utf-8")
    plain = tmp_path / "plain"
    plain.mkdir()
    assert pre_ci_check._mypy_cache_key(repo, plain, "") is None


def _install_fake_mypy(monkeypatch, results):
    """Fake only the mypy invocation; git calls still run for real."""
    real_run = subprocess.run
    calls: list[tuple[list[str], dict[str, str]]] = []

    def fake_run(cmd, **kw):
        if "--python-version" in cmd:
            calls.append((list(cmd), dict(kw.get("env") or {})))
            out, rc = results.pop(0) if len(results) > 1 else results[0]
            return subprocess.CompletedProcess(cmd, rc, out, "")
        return real_run(cmd, **kw)

    monkeypatch.setattr(pre_ci_check.shutil, "which", lambda _: "/usr/bin/mypy")
    monkeypatch.setattr(pre_ci_check, "_triton_numpy_spec",
                        lambda label="mypy": "")
    monkeypatch.setattr(pre_ci_check.subprocess, "run", fake_run)
    return calls


def _mypy_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "ascend"
    _init_repo(repo)
    (repo / "mypy.ini").write_text("[mypy]\n", encoding="utf-8")
    vllm = tmp_path / "vllm"
    _init_repo(vllm)
    return repo, vllm


def test_check_mypy_fresh_cache_disables_incremental(
        monkeypatch, tmp_path: Path) -> None:
    repo, vllm = _mypy_repo(tmp_path)
    calls = _install_fake_mypy(monkeypatch, [("", 0)])

    result = pre_ci_check._check_mypy(repo, vllm, fresh_cache=True)

    assert result["violations"] == []
    assert len(calls) == 3
    for cmd, env in calls:
        assert cmd[-1] == "vllm_ascend"
        assert "--no-incremental" in cmd
        assert "MYPY_CACHE_DIR" not in env


def test_check_mypy_persistent_cache_is_per_tree(
        monkeypatch, tmp_path: Path) -> None:
    repo, vllm = _mypy_repo(tmp_path)
    release = tmp_path / "vllm-release"
    _init_repo(release)
    # Distinct HEAD: identical temp repos can produce the same commit sha
    # when created in the same second, which would share one bucket.
    (release / "vllm_ascend" / "code.py").write_text("x = 9\n",
                                                     encoding="utf-8")
    subprocess.run(["git", "commit", "-qam", "release"], cwd=str(release),
                   check=True, capture_output=True, text=True)
    calls = _install_fake_mypy(monkeypatch, [("", 0)])

    pre_ci_check._check_mypy(repo, vllm, vllm_release_path=release)

    # main x3 (3.10/3.11/3.12) + release x1 (3.10)
    assert len(calls) == 4
    dirs = [env.get("MYPY_CACHE_DIR") for _, env in calls]
    assert all("--no-incremental" not in cmd for cmd, _ in calls)
    assert len(set(dirs[:3])) == 1
    assert dirs[3] and dirs[3] != dirs[0]


def test_check_mypy_filter_sigs_ignore_runner_noise(
        monkeypatch, tmp_path: Path) -> None:
    # The full tests tree exits non-zero on this runner for mock-pattern
    # noise; only a missing vllm module is a real violation.
    repo, vllm = _mypy_repo(tmp_path)
    noise = ('tests/ut/a.py:3: error: Unexpected keyword argument "x"  '
             "[call-arg]")
    real = ('tests/ut/models/test_glm5_next_mtp.py:8: error: Cannot find '
            'implementation or library stub for module named '
            '"vllm.transformers_utils.configs.glm5_next"  [import-not-found]')
    calls = _install_fake_mypy(monkeypatch, [(noise + "\n" + real, 1)])

    result = pre_ci_check._check_mypy(
        repo, vllm, fresh_cache=True, targets=("examples", "tests"),
        filter_sigs=fqg._VLLM_MISSING_MODULE_SIGS,
        python_versions=("3.10",))

    assert len(calls) == 1
    assert calls[0][0][-2:] == ["examples", "tests"]
    assert result["violations"] == [real]


def test_check_mypy_filter_sigs_noise_only_is_clean(
        monkeypatch, tmp_path: Path) -> None:
    repo, vllm = _mypy_repo(tmp_path)
    noise = 'tests/ut/a.py:3: error: Unexpected keyword argument "x"  [call-arg]'
    _install_fake_mypy(monkeypatch, [(noise, 1)])

    result = pre_ci_check._check_mypy(
        repo, vllm, fresh_cache=True, targets=("tests",),
        filter_sigs=fqg._VLLM_MISSING_MODULE_SIGS,
        python_versions=("3.10",))

    assert result["violations"] == []
    assert not result.get("skipped")


def test_check_mypy_filter_sigs_fatal_exit_is_not_clean(
        monkeypatch, tmp_path: Path) -> None:
    # exit 2 = fatal (unreadable mypy.ini / crash); filtering noise must
    # not turn a crashed audit into a green gate.
    repo, vllm = _mypy_repo(tmp_path)
    _install_fake_mypy(monkeypatch, [("mypy: error: Cannot read mypy.ini", 2)])

    result = pre_ci_check._check_mypy(
        repo, vllm, fresh_cache=True, targets=("tests",),
        filter_sigs=fqg._VLLM_MISSING_MODULE_SIGS,
        python_versions=("3.10",))

    assert result["violations"] == ["mypy: error: Cannot read mypy.ini"]


# --- final gate wiring -----------------------------------------------------

def test_final_gate_runs_fresh_mypy_and_tests_import_audit(
        monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)
    calls = []

    def fake_mypy(repo_arg, vllm_arg, release_arg=None, **kw):
        calls.append(kw)
        return {"violations": [], "detail": "mypy clean"}

    monkeypatch.setattr(fqg, "_check_format",
                        lambda r: {"violations": [], "detail": "clean"})
    monkeypatch.setattr(fqg, "_check_mypy", fake_mypy)
    monkeypatch.setattr(fqg, "_check_ut",
                        lambda *a, **k: {"violations": [], "detail": "clean"})

    passed, errs = run_final_quality_gate(repo, tmp_path / "vllm", tmp_path)

    assert passed and errs == []
    assert len(calls) == 2
    assert calls[0]["fresh_cache"] is True
    audit = calls[1]
    assert audit["fresh_cache"] is True
    assert audit["targets"] == ("examples", "tests")
    assert audit["filter_sigs"] == fqg._VLLM_MISSING_MODULE_SIGS
    assert audit["python_versions"] == ("3.10",)
    gate = json.loads((tmp_path / "quality_gate.json").read_text(
        encoding="utf-8"))
    assert [c["name"] for c in gate["checks"]] == [
        "format", "mypy", "mypy-tests-imports", "ut"]


def test_final_gate_fails_on_missing_vllm_import_in_tests(
        monkeypatch, tmp_path: Path) -> None:
    repo = tmp_path / "ascend"
    _init_repo(repo)
    violation = ('tests/ut/models/test_glm5_next_mtp.py:8: error: Cannot '
                 'find implementation or library stub for module named '
                 '"vllm.transformers_utils.configs.glm5_next"  '
                 '[import-not-found]')

    def fake_mypy(repo_arg, vllm_arg, release_arg=None, **kw):
        if kw.get("targets"):
            return {"violations": [violation],
                    "detail": "1 mypy issue(s) (main/3.10)"}
        return {"violations": [], "detail": "mypy clean"}

    monkeypatch.setattr(fqg, "_check_format",
                        lambda r: {"violations": [], "detail": "clean"})
    monkeypatch.setattr(fqg, "_check_mypy", fake_mypy)
    monkeypatch.setattr(fqg, "_check_ut",
                        lambda *a, **k: {"violations": [], "detail": "clean"})

    passed, errs = run_final_quality_gate(repo, tmp_path / "vllm", tmp_path)

    assert not passed and errs
    gate = json.loads((tmp_path / "quality_gate.json").read_text(
        encoding="utf-8"))
    bad = next(c for c in gate["checks"] if c["name"] == "mypy-tests-imports")
    assert bad["passed"] is False
    assert bad["violations"] == [violation]


def test_check_format_timeout_is_not_retried(monkeypatch,
                                             tmp_path: Path) -> None:
    """A hung format.sh fails once instead of hanging twice."""
    repo = tmp_path / "ascend"
    repo.mkdir()
    (repo / "format.sh").write_text("#!/bin/bash\n", encoding="utf-8")
    calls: list[int] = []

    def fake_run(repo_arg):
        calls.append(1)
        return subprocess.CompletedProcess(
            ["bash"], pre_ci_check.FORMAT_TIMEOUT_RC,
            "ruff-check...\n", "timed out after 1800s")

    monkeypatch.setattr(pre_ci_check.shutil, "which",
                        lambda name: "/usr/bin/pre-commit")
    monkeypatch.setattr(pre_ci_check, "run_format_sh", fake_run)

    result = pre_ci_check._check_format(repo)

    assert len(calls) == 1
    assert "TIMED OUT" in result["detail"]
    assert any("timed out" in v for v in result["violations"])
