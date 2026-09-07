"""CPU-UT batch runner for the main2main quality gate.

Standalone module (not embedded in pre_ci_check.py): collects the
CPU-routed ``tests/ut`` files and runs them in a single-process pytest
batch against the target main vllm checkout, mirroring vllm-ascend's
``run_selected_tests.sh`` cpu-ut batch.

Key mechanisms:
- **Fake npu-smi on the PATH**: vllm-ascend's tests/ut/conftest.py checks
  ``npu-smi info`` to decide whether to mock torch_npu.  A fake npu-smi
  (exit 1) forces the mock path even on an NPU runner, exactly like CI's
  CPU runner.
- **Single-process batch**: all files in one pytest process (CI-aligned,
  fast); files known to pollute the shared process (module-level
  monkeypatches without cleanup) run in their own subprocess.
- **--continue-on-collection-errors**: one file failing to import no
  longer aborts the whole batch and masks every other test.
- **ut_namespace plugin**: PYTHONPATH=<ascend>:<vllm> makes
  vllm's regular ``examples/`` package shadow ascend's namespace
  ``examples/`` — the plugin pre-registers the ascend dir so collection
  matches real CI (vllm installed, no examples/ on sys.path).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from main2main_flow.scripts.utils.utils import (
    WORKSPACE_DIR,
    pip_install_with_fallback,
    ts_print,
)

_BALANCE_TAG_BODY_TEST = "test_schedule_body_matches_pinned_release_tag"

_CPU_UT_A2_RE = re.compile(r"tests/ut/.+/a2(/|$)")
_CPU_UT_A3_2_RE = re.compile(r"tests/ut/.+/a3_2(/|$)")


def _is_npu_convention_ut_path(rel_path: str) -> bool:
    """Mirror select_tests._route_ut_dir — True if path routes to NPU runner."""
    p = rel_path.replace("\\", "/")
    return bool(_CPU_UT_A2_RE.search(p) or _CPU_UT_A3_2_RE.search(p))


def _collect_cpu_ut_files(repo: Path) -> list[str]:
    """Return CPU-routed tests/ut paths (rel to repo).

    Routes from vllm-ascend's OWN ``test_config.yaml`` — the same
    ``runner_mapping`` + ``skip_tests`` CI's select_tests.py reads — so the
    set tracks CI automatically when routing changes (new NPU directories,
    new skip entries).  Reading the config is read-only; vllm-ascend is
    never modified.  Falls back to the convention regexes when the config
    is missing or unparseable (old checkouts).
    """
    skip_tests: set[str] = set()
    npu_patterns: list[re.Pattern] = []
    config_path = repo / ".github/workflows/scripts/test_config.yaml"
    if config_path.exists():
        try:
            import yaml
            docs = list(yaml.safe_load_all(
                config_path.read_text(encoding="utf-8")))
            # Two formats in the wild: OLD = doc0 list-of-module-dicts +
            # doc1 meta dict; NEW (upstream main) = ONE dict doc with
            # top-level skip_tests / runner_mapping / estimated_times.
            meta: dict = {}
            for doc in docs:
                if isinstance(doc, list):
                    for module in doc:
                        if isinstance(module, dict):
                            for s in module.get("skip_tests", []) or []:
                                skip_tests.add(str(s).rstrip("/"))
                elif isinstance(doc, dict):
                    if "runner_mapping" in doc:
                        meta = doc
                    for s in doc.get("skip_tests", []) or []:
                        if isinstance(s, str):
                            skip_tests.add(s.rstrip("/"))
            for pattern_str in (meta.get("runner_mapping", {}) or {}):
                if pattern_str.startswith("tests/ut"):
                    npu_patterns.append(re.compile(pattern_str))
            if npu_patterns:
                ts_print(f"[pre_ci] ut: routing from test_config.yaml "
                         f"({len(npu_patterns)} NPU pattern(s), "
                         f"{len(skip_tests)} skip entry(s))")
        except Exception as e:
            ts_print(f"[pre_ci] ut: failed to parse test_config.yaml ({e}), "
                     "falling back to convention regexes")
            npu_patterns = []

    ut_dir = repo / "tests" / "ut"
    if not ut_dir.exists():
        return []
    files: list[str] = []
    for root, dirs, fnames in os.walk(ut_dir):
        if "__pycache__" in dirs:
            dirs.remove("__pycache__")
        for f in sorted(fnames):
            if f.startswith("test_") and f.endswith(".py"):
                rel = os.path.relpath(os.path.join(root, f), str(repo))
                if skip_tests and rel in skip_tests:
                    continue
                if npu_patterns:
                    if any(p.search(rel) for p in npu_patterns):
                        continue
                elif _is_npu_convention_ut_path(rel):
                    continue
                files.append(rel)
    return files


_EXCERPT_SIG_RE = re.compile(
    r"Traceback \(most recent call last\)|Traceback:"
    r"|\b(?:ValueError|RuntimeError|TypeError|KeyError|AttributeError|"
    r"AssertionError|ImportError|IndexError|OverflowError|OSError|"
    r"NameError|NotImplementedError|EngineDeadError)\b")


def _failure_excerpt(clean: str, failure_line: str, max_chars: int = 900) -> str:
    """Extract a traceback window around a failing test's error message.

    The gate's violations previously carried only the one-line pytest
    summary ("TypeError: 'NoneType' object is not iterable") — the adapter
    had to guess where and why.  Locate the error message in the full
    pytest output and return the surrounding window (the code line that
    raised, plus the tail of the call stack).
    """
    err = failure_line.split(" - ", 1)[-1] if " - " in failure_line else failure_line
    needle = err.strip()[:80]
    idx = clean.find(needle)
    if idx < 0:
        m = _EXCERPT_SIG_RE.search(clean)
        if not m:
            return ""
        idx = m.start()
    start = max(0, idx - 200)
    end = min(len(clean), idx + max_chars)
    excerpt = clean[start:end].strip()
    # 截断到下一个测试标题/分隔（pytest 的 ____ name ____ 或 ==== 段）。
    for marker in ("\n____", "\n===", "\n---------"):
        cut = excerpt.find(marker, 1)
        if cut > 0:
            excerpt = excerpt[:cut]
            break
    return excerpt or ""


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

UT_FULL_LOG_NAME = "ut_full.log"
_UT_VENV_ENV = "MAIN2MAIN_UT_VENV"
_UT_VENV_MARKER = "m2m_meta.json"


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _ut_base_dir() -> Path:
    """Home of the persistent UT venv and its full-run logs."""
    env = os.environ.get(_UT_VENV_ENV, "")
    return Path(env) if env else WORKSPACE_DIR / "ut_venv"


def _triton_numpy_spec() -> str:
    """numpy constraint from triton-ascend metadata (pins the UT venv).

    "" when triton-ascend is absent/unparseable — the venv then has no
    numpy pin beyond --system-site-packages.
    """
    import importlib.metadata as _md
    try:
        from packaging.requirements import Requirement
        reqs = _md.requires("triton-ascend") or []
        for req in reqs:
            if "extra" in req.lower():
                continue
            try:
                r = Requirement(req)
            except Exception:
                continue
            if r.name.lower() == "numpy":
                return ",".join(
                    f"{s.operator}{s.version}" for s in r.specifier)
    except Exception as e:
        ts_print(f"[pre_ci] ut: failed to read triton-ascend numpy "
                 f"constraint ({e})")
    return ""


def _ensure_ut_venv(target_numpy_spec: str) -> tuple[Path | None, str]:
    """Create-or-reuse the persistent UT venv; return (venv_dir, venv_python).

    The venv lives across pre_ci attempts and steps (the runner is
    ephemeral, so no end-of-run cleanup is needed).  Reuse requires
    bin/python to exist AND the numpy spec recorded in the marker to
    match what the current triton-ascend metadata asks for.  Any
    failure falls back to the system pytest (returns (None, "")).
    """
    venv_dir = _ut_base_dir()
    venv_python = venv_dir / "bin" / "python"
    if venv_python.exists():
        try:
            meta = json.loads((venv_dir / _UT_VENV_MARKER).read_text(
                encoding="utf-8"))
            if meta.get("numpy_spec") == target_numpy_spec:
                ts_print(f"[pre_ci] ut: reusing persistent venv at {venv_dir}")
                return venv_dir, str(venv_python)
            ts_print(f"[pre_ci] ut: persistent venv numpy spec mismatch "
                     f"({meta.get('numpy_spec')!r} != "
                     f"{target_numpy_spec!r}) — recreating")
        except Exception:
            ts_print("[pre_ci] ut: persistent venv marker unreadable "
                     "— recreating")
        shutil.rmtree(venv_dir, ignore_errors=True)

    ts_print(f"[pre_ci] ut: creating persistent venv at {venv_dir} "
             f"(numpy{target_numpy_spec} from triton-ascend)")
    try:
        venv_dir.mkdir(parents=True, exist_ok=True)
        r = subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir),
             "--system-site-packages"],
            capture_output=True, text=True, timeout=180,
        )
        if r.returncode != 0:
            ts_print("[pre_ci] ut: WARNING venv creation FAILED — "
                     "falling back to system pytest")
            return None, ""
        if target_numpy_spec:
            try:
                r2 = pip_install_with_fallback(
                    venv_python, ["-q", f"numpy{target_numpy_spec}"])
            except subprocess.TimeoutExpired:
                ts_print("[pre_ci] ut: WARNING numpy install TIMED OUT — "
                         "falling back to system pytest")
                r2 = None
            if r2 is not None and r2.returncode != 0:
                ts_print("[pre_ci] ut: WARNING numpy install FAILED "
                         f"({r2.stderr.strip()[:200]}) — falling back "
                         "to system pytest")
                return None, ""
        (venv_dir / _UT_VENV_MARKER).write_text(
            json.dumps({"numpy_spec": target_numpy_spec}),
            encoding="utf-8")
        return venv_dir, str(venv_python)
    except subprocess.TimeoutExpired:
        ts_print("[pre_ci] ut: WARNING venv creation TIMED OUT (180s) — "
                 "falling back to system pytest")
        return None, ""


def _make_fake_npu_smi() -> Path:
    """Fake npu-smi (exit 1) so tests/ut/conftest.py takes the mock path
    even on an NPU runner — otherwise CPU UT cases hit real NPU ops."""
    fake_bin_dir = Path(tempfile.mkdtemp(prefix="ut_fake_bin_"))
    npu_smi_fake = fake_bin_dir / "npu-smi"
    try:
        npu_smi_fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        npu_smi_fake.chmod(0o755)
    except OSError:
        pass
    return fake_bin_dir


def _build_ut_env(repo: Path, vllm_path: str | Path, fake_bin_dir: Path) -> dict:
    """Env replicating CI's CPU-UT lane: pure CPU, mocked NPU, offline hub.

    The flow repo root is appended to PYTHONPATH so the ut_namespace
    plugin resolves even when the caller's environment doesn't carry it.
    """
    env = os.environ.copy()
    ascend_abs = str(repo.resolve())
    vllm_abs = str(Path(vllm_path).resolve())
    flow_root = str(Path(__file__).resolve().parents[3])
    existing = env.get("PYTHONPATH", "")
    parts = [ascend_abs, vllm_abs]
    if existing:
        parts.append(existing)
    parts.append(flow_root)
    env["PYTHONPATH"] = ":".join(parts)
    env["TORCH_DEVICE_BACKEND_AUTOLOAD"] = "0"
    env["VLLM_WORKER_MULTIPROC_METHOD"] = "spawn"
    # Match CI: offline mode so get_model_file / hf_hub_download
    # fails immediately (385s→0.5s for test_maybe_update_config_
    # non_directory_raises) instead of retrying network timeouts.
    env["HF_HUB_OFFLINE"] = "1"
    env["VLLM_USE_MODELSCOPE"] = "True"
    env["PATH"] = f"{fake_bin_dir}:{env.get('PATH', '')}"
    # Hide NPU so platform detection sees pure CPU — matches
    # PR CI cpu-0.  fake npu-smi still mocks conftest, but
    # torch_npu's runtime sees no visible devices.
    env["ASCEND_RT_VISIBLE_DEVICES"] = ""
    env.pop("CUDA_VISIBLE_DEVICES", None)
    return env


def check_ut(repo: Path, vllm_path: str | Path | None = None) -> dict:
    """Run the CPU-UT batch, aligned with CI's single-process execution.

    Runs the same CPU-routed tests/ut/* files as CI's CPU runner
    (linux-amd64-cpu-8-hk), but on whatever machine main2main runs on.
    Mirrors vllm-ascend's ``run_selected_tests.sh`` cpu-ut batch: ALL
    files in ONE pytest process (CI runs 2044 tests in ~44s; per-file
    subprocess isolation cost ~5 min per version).

    Runs the batch against the target main vllm checkout (``vllm_path``)
    only — single-version validation.

    ``test_schedule_body_matches_pinned_release_tag`` is excluded —
    see ``_BALANCE_TAG_BODY_TEST``.

    Env mirrors CI: venv with --system-site-packages + numpy==1.26.4
    (from triton-ascend metadata) + PYTHONPATH=ascend:vllm.  torch_npu
    is mocked by conftest, so the venv python's C-extension issue that
    broke A2-NPU-UT in PR #13657 does not apply here.

    Returns dict with ``violations`` (failing test node IDs) and
    ``detail``.  Empty violations + non-skipped → pass.
    """
    cpu_files = _collect_cpu_ut_files(repo)
    if not cpu_files:
        ts_print("\n[pre_ci] ut: SKIPPED — tests/ut not found or no CPU tests")
        return {"violations": [], "detail": "tests/ut not found", "skipped": True,
                "log_path": "", "venv_python": ""}

    if not vllm_path:
        ts_print("\n[pre_ci] ut: SKIPPED — no vllm path configured")
        return {"violations": [], "detail": "no vllm path", "skipped": True,
                "log_path": "", "venv_python": ""}

    ts_print(f"\n[pre_ci] ut: collected {len(cpu_files)} CPU test files "
             f"(per-file isolation, NPU-convention a2/ and a3_2/ excluded)")

    # Read numpy constraint from triton-ascend metadata (mirror _check_mypy).
    target_numpy_spec = _triton_numpy_spec()

    # Persistent venv (created once, reused across attempts and steps) with
    # --system-site-packages + the numpy constraint.
    venv_dir, venv_python = _ensure_ut_venv(target_numpy_spec)
    # Resolve the system pytest only AFTER the venv: the venv runs pytest
    # via `python -m pytest` (system-site-packages), so a missing pytest
    # console script on PATH does not mean UT cannot run.
    pytest_bin = shutil.which("pytest")
    if not venv_dir and not pytest_bin:
        ts_print("\n[pre_ci] ut: SKIPPED — no usable venv and pytest not installed")
        return {"violations": [], "detail": "pytest not installed", "skipped": True,
                "log_path": "", "venv_python": ""}
    pytest_cmd = [str(venv_python), "-m", "pytest"] if venv_dir else [pytest_bin]
    if venv_dir:
        ts_print(f"[pre_ci] ut: using venv pytest via {venv_python} -m pytest")

    # Full pytest stdout is persisted here so the adapter (fix mode) can
    # grep complete tracebacks when a violation's excerpt was truncated.
    log_path = (venv_dir or WORKSPACE_DIR) / UT_FULL_LOG_NAME

    fake_bin_dir = _make_fake_npu_smi()

    all_violations: list[str] = []
    all_files_clean = True
    details: list[str] = []
    failed_re = re.compile(r"^(FAILED|ERROR)\s+(\S+\.py::\S+)")

    try:
        label = "main"

        env = _build_ut_env(repo, vllm_path, fake_bin_dir)
        ts_print(f"[pre_ci] ut: [{label}] pure-CPU env "
                 f"(ASCEND_RT_VISIBLE_DEVICES='')")

        ts_print(f"\n[pre_ci] ut: === batch [{label}] "
                 f"PYTHONPATH={env['PYTHONPATH']} ===")

        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            "# pre_ci UT full log\n"
            f"# pytest cmd: {' '.join(pytest_cmd)}\n"
            f"# repo: {repo.resolve()}\n"
            f"# vllm: {Path(vllm_path).resolve()}\n"
            f"# PYTHONPATH: {env['PYTHONPATH']}\n",
            encoding="utf-8")

        # Files known to pollute the shared process get their own
        # subprocess.  Verified on the A2 env: test_batch_invariant.py
        # installs a global torch.library.Library monkeypatch that breaks
        # test_gdn_layerwise_kv.py when run in the same process.
        # test_vocab_parallel_embedding.py assigns module-level
        # parallel_state._MLP_TP/_OTP = MagicMock without cleanup,
        # polluting test_linear.py / test_gdn_layerwise_kv.py in the
        # same process (verified on A2, run 2026-08-12).
        # test_gdn_layerwise_kv.py itself fails only inside the batch
        # (qwen_gdn_attention_core CPU-backend NotImplementedError;
        # passes standalone) — isolate it too so the batch stays clean.
        isolated = [f for f in cpu_files
                    if f.endswith(("test_batch_invariant.py",
                                   "test_vocab_parallel_embedding.py",
                                   "test_gdn_layerwise_kv.py"))]
        batch = [f for f in cpu_files if f not in isolated]

        exclude_expr = f"not {_BALANCE_TAG_BODY_TEST}"

        runs: list[tuple[str, subprocess.CompletedProcess]] = []
        try:
            # --continue-on-collection-errors: a single file that fails
            # to import (e.g. an env-specific ModuleNotFoundError) must
            # NOT abort the whole batch and mask every other test —
            # the batch is one pytest process for all files (run
            # 31563761175: sfa_pd_rd2h collection error hid 8 real
            # regressions that PR CI then exposed).
            # -p ut_namespace: PYTHONPATH=<ascend>:<vllm>
            # makes vllm's regular examples/ package shadow ascend's
            # namespace examples/ — pre-register the ascend dir so the
            # batch matches real CI (vllm installed, no examples/ on
            # sys.path).
            rr = subprocess.run(
                [*pytest_cmd, "-q", "--tb=short", "--no-header",
                 "--continue-on-collection-errors",
                 "-p", "main2main_flow.scripts.utils.ut_namespace",
                 *batch, "-k", exclude_expr],
                cwd=str(repo), capture_output=True, text=True,
                env=env, timeout=1200,
            )
            runs.append(("batch", rr))
        except subprocess.TimeoutExpired:
            ts_print(f"[pre_ci] ut: [{label}] batch TIMEOUT(1200s)")
            all_files_clean = False
            details.append(f"{label}/batch: TIMEOUT(1200s)")
        for f in isolated:
            try:
                rr = subprocess.run(
                    [*pytest_cmd, "-q", "--tb=short", "--no-header", f],
                    cwd=str(repo), capture_output=True, text=True,
                    env=env, timeout=300,
                )
                runs.append((f, rr))
            except subprocess.TimeoutExpired:
                ts_print(f"[pre_ci] ut: [{label}] {f} TIMEOUT(300s)")
                all_files_clean = False

        for name, rr in runs:
            clean = strip_ansi(rr.stdout + rr.stderr)
            with log_path.open("a", encoding="utf-8") as lf:
                lf.write(f"\n===== run: {name} (exit={rr.returncode}) =====\n")
                lf.write(clean)
            seen: set[str] = set()
            for line in clean.splitlines():
                m = failed_re.search(line.strip())
                if m and m.group(2) not in seen:
                    seen.add(m.group(2))
                    v = f"[{label}] {line.strip()}"
                    ex = _failure_excerpt(clean, line.strip())
                    if ex:
                        v += "\n" + ex
                    all_violations.append(v)
            if rr.returncode != 0:
                all_files_clean = False
                if not seen:
                    all_violations.append(
                        f"[{label}] {name}: exit={rr.returncode} — "
                        f"{clean[-500:]}")
            summary_m = re.search(
                r"((?:\d+ failed, )?\d+ passed[^\n]*)", clean)
            summary = (summary_m.group(1) if summary_m
                       else f"exit={rr.returncode}")
            details.append(f"{label}/{name}: {summary}")
            ts_print(f"[pre_ci] ut: [{label}/{name}] {summary}")

        if all_files_clean:
            ts_print(f"\n[pre_ci] ut: OK — all {len(cpu_files)} files clean")
            return {"violations": [],
                    "detail": f"UT clean ({len(cpu_files)} files, "
                              f"single-process batch)",
                    "log_path": str(log_path),
                    "venv_python": str(venv_python) if venv_dir else ""}
        ts_print(f"\n[pre_ci] ut: {len(all_violations)} failure(s):")
        for v in all_violations[:20]:
            ts_print(f"  {v}")
        if len(all_violations) > 20:
            ts_print(f"  ... and {len(all_violations) - 20} more")
        return {"violations": all_violations,
                "detail": f"{len(all_violations)} UT failure(s): "
                          + "; ".join(details),
                "log_path": str(log_path),
                "venv_python": str(venv_python) if venv_dir else ""}
    finally:
        # The venv persists across attempts/steps (recreated only on numpy
        # spec mismatch) — only the throwaway fake-bin dir is deleted here.
        if fake_bin_dir.exists():
            shutil.rmtree(fake_bin_dir, ignore_errors=True)
