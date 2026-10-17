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
- **Single-process batch**: ALL files in one pytest process (CI-aligned,
  fast) in pytest's collection order — no per-file isolation list, which
  would rot as upstream's tests evolve (see the order note in
  _collect_cpu_ut_files and the polluter note in check_ut).
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
import time
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
    # Sort the batch into pytest's collection order (recursively name-sorted
    # with directories interleaved among files).  os.walk hands back
    # root-level files BEFORE subdirectories, which inverts pytest's order:
    # ``test_logger.py`` then runs early in the batch while
    # ``runtime_config/`` runs after it.  That order breaks the batch:
    # test_logger's file-logging test leaks a handler bound to its deleted
    # TemporaryDirectory on the ``vllm_ascend`` logger, and a later
    # ``configure_ascend_file_logging()`` closes the module handler, so
    # every subsequent log emit re-opens the dead stream and raises
    # FileNotFoundError (parity run 38405850804: 371 violations on the
    # os.walk order vs 1 failure in pytest's own order on the SAME tree).
    # Sorting matches upstream CI's single-process order and keeps the
    # polluter-then-reconfigurer inversion out of the batch.
    files.sort()
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
    if " - " in failure_line:
        node_part, err = failure_line.split(" - ", 1)
    else:
        node_part, err = "", failure_line
    needle = err.strip()[:80]
    idx = clean.find(needle)
    # The error text can occur EARLIER in the run than the failing test
    # (two tests raising the same error): clean.find() then quotes another
    # test's traceback — run 38405850804's request_lifecycle violations
    # carried a build_cache test's excerpt, which sent the root-cause
    # analysis to the wrong file.  Anchor the search at the failing
    # test's own pytest failure-section header when it exists.
    section_start = 0
    if idx >= 0 and node_part:
        test_name = node_part.split("::")[-1]
        # pytest renders each failure under a `_____ name _____` header;
        # methods show `Class.name`, parametrized add `[id]` — substring
        # match covers both.
        section_m = None
        for hm in re.finditer(r"^_{5,}.*_{5,}$", clean, re.MULTILINE):
            if test_name in hm.group(0):
                section_m = hm
                break
        if section_m:
            anchored = clean.find(needle, section_m.start())
            if anchored >= 0:
                idx = anchored
                # Keep the context window inside this test's section —
                # a 200-char reach backward must not quote the previous
                # test's traceback.
                section_start = section_m.start()
            # else: needle wording differs between the short summary and
            # the traceback — keep the first-occurrence idx.
    if idx < 0:
        m = _EXCERPT_SIG_RE.search(clean)
        if not m:
            return ""
        idx = m.start()
    start = max(section_start, idx - 200)
    end = min(len(clean), idx + max_chars)
    excerpt = clean[start:end].strip()
    # 截断到下一个测试标题/分隔（pytest 的 ____ name ____ 或 ==== 段）。
    for marker in ("\n____", "\n===", "\n---------"):
        cut = excerpt.find(marker, 1)
        if cut > 0:
            excerpt = excerpt[:cut]
            break
    return excerpt or ""


def _unparseable_evidence(clean: str, max_chars: int = 900) -> str:
    """Extract the actual error from pytest output with no parseable
    FAILED/ERROR line (batch died before the summary — conftest or
    startup crash).  Prefers the traceback block ending at pytest's last
    ``E   ...`` exception line over the stdout tail: the tail is the
    warnings summary, which is how run 34583706211's final blocker
    became information-free."""
    lines = clean.splitlines()
    last_e = max((i for i, ln in enumerate(lines)
                  if ln.startswith("E   ")), default=-1)
    if last_e >= 0:
        start = next((i for i in range(last_e, -1, -1)
                      if lines[i].startswith("Traceback")), last_e - 15)
        return "\n".join(lines[max(0, start):last_e + 1])[-max_chars:].strip()
    m = list(re.finditer(r"^Traceback \(most recent call last\)", clean,
                         re.MULTILINE))
    if m:
        return clean[m[-1].start():][-max_chars:].strip()
    return clean[-500:]


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


def _triton_numpy_spec(label: str = "ut") -> str:
    """numpy constraint from triton-ascend metadata (pins the UT venv).

    "" when triton-ascend is absent/unparseable — the venv then has no
    numpy pin beyond --system-site-packages.  *label* only names the log
    prefix (pre_ci_check's mypy lint reuses this reader).
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
        ts_print(f"[pre_ci] {label}: failed to read triton-ascend numpy "
                 f"constraint ({e})")
    return ""


def _venv_numpy_version(venv_python: Path | str) -> str:
    """numpy version actually importable inside the venv ("" if none).

    The probe runs the venv python itself, so the resolution it reports
    (--system-site-packages: venv site-packages first, then system) is
    exactly what mypy/pytest will see.
    """
    try:
        r = subprocess.run(
            [str(venv_python), "-c",
             "import numpy; print(numpy.__version__)"],
            capture_output=True, text=True, timeout=30)
        return r.stdout.strip() if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _numpy_drifted(spec: str, installed: str) -> bool:
    """True when a venv's numpy no longer satisfies its pin.

    The marker records only the *spec*, so a venv whose numpy really
    lives in system site-packages (pip skipped the install as "already
    satisfied" behind --system-site-packages) drifts silently when the
    e2e vllm install upgrades system numpy — run 38220121641 12:02:
    1.26.4 -> 2.3.5, three lint rounds of spurious [var-annotated]
    errors.  Drift here means "recreating can fix it": an unimportable
    numpy, or one outside the spec.  A 2.x the spec admits is NOT drift
    (rebuilding would reinstall the same thing; _check_mypy keeps
    warning about that case instead).
    """
    if not installed:
        return True
    try:
        from packaging.specifiers import SpecifierSet
        return not SpecifierSet(spec).contains(installed)
    except Exception:
        return False


def _ensure_ut_venv(target_numpy_spec: str) -> tuple[Path | None, str]:
    """Create-or-reuse the persistent UT venv; return (venv_dir, venv_python).

    The venv lives across pre_ci attempts and steps (the runner is
    ephemeral, so no end-of-run cleanup is needed).  Reuse requires
    bin/python to exist AND the numpy spec recorded in the marker to
    match what the current triton-ascend metadata asks for AND the
    actually importable numpy to satisfy it — the marker can look
    healthy while the venv never owned its numpy (pip "already
    satisfied" behind --system-site-packages) and drifts with system
    upgrades (run 38220121641).  Any failure falls back to the system
    pytest (returns (None, "")).
    """
    venv_dir = _ut_base_dir()
    venv_python = venv_dir / "bin" / "python"
    if venv_python.exists():
        reason = ""
        try:
            meta = json.loads((venv_dir / _UT_VENV_MARKER).read_text(
                encoding="utf-8"))
            if meta.get("numpy_spec") != target_numpy_spec:
                reason = (f"numpy spec mismatch ({meta.get('numpy_spec')!r} "
                          f"!= {target_numpy_spec!r})")
        except Exception:
            reason = "marker unreadable"
        if not reason and target_numpy_spec:
            # Probe the ACTUAL numpy, not just the marker.
            installed = _venv_numpy_version(venv_python)
            if _numpy_drifted(target_numpy_spec, installed):
                reason = (f"installed numpy {installed or 'MISSING'} "
                          f"violates numpy{target_numpy_spec}")
        if reason:
            ts_print(f"[pre_ci] ut: persistent venv {reason} — recreating")
            shutil.rmtree(venv_dir, ignore_errors=True)
        else:
            ts_print(f"[pre_ci] ut: reusing persistent venv at {venv_dir}")
            return venv_dir, str(venv_python)

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
            # --ignore-installed: behind --system-site-packages a satisfying
            # system numpy turns this into a pip no-op ("already satisfied")
            # and the venv never owns its pin — the e2e vllm install then
            # upgrades system numpy out from under it (run 38220121641).
            # Forcing the wheel into the venv makes the pin immune to
            # system-side flips.
            try:
                r2 = pip_install_with_fallback(
                    venv_python,
                    ["-q", "--ignore-installed",
                     f"numpy{target_numpy_spec}"])
            except subprocess.TimeoutExpired:
                ts_print("[pre_ci] ut: WARNING numpy install TIMED OUT — "
                         "falling back to system pytest")
                r2 = None
            if r2 is not None and r2.returncode != 0:
                ts_print("[pre_ci] ut: WARNING numpy install FAILED "
                         f"({r2.stderr.strip()[:200]}) — falling back "
                         "to system pytest")
                return None, ""
            # pip exiting 0 is not proof the wheel landed in the venv;
            # the probe catches any remaining silent no-op.
            if _numpy_drifted(target_numpy_spec,
                              _venv_numpy_version(venv_python)):
                ts_print(f"[pre_ci] ut: WARNING post-install numpy probe "
                         f"violates numpy{target_numpy_spec} — falling "
                         "back to system pytest")
                shutil.rmtree(venv_dir, ignore_errors=True)
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


# What upstream's cpu-UT lane gets from a SOC-less build: setup.py's
# gen_build_info() writes the device family resolved from SOC_VERSION, and
# on the CPU runner every SOC step is gated `npu_type != 'cpu'`, so
# _build_info.py carries the no-SOC default — device_config.py maps a
# missing __device_type__/ASCEND910B1 to AscendDeviceType.A2.
_PURE_CPU_DEVICE_TYPE = "A2"
_BUILD_INFO_DEVICE_TYPE_RE = re.compile(
    r"^(__device_type__\s*=\s*)['\"][^'\"]*['\"]", re.MULTILINE)


def _swap_pure_cpu_build_info(repo: Path) -> tuple[Path | None, str | None]:
    """Point the tree's own ``_build_info`` device type at the CPU default.

    The device profile is baked at BUILD time: ``_build_info.py`` is
    generated in-tree by the install (a3 SOC here) and
    ``get_current_hardware_profile()`` reads it at import — no env var can
    change it.  The production a3 install is correct for the e2e lanes,
    but the UT batch must simulate the same pure-CPU environment as
    upstream's cpu-UT lane (device semantics A2), otherwise unpatched
    profile-dependent tests diverge from CI for build reasons that have
    nothing to do with the code under test (run 38412395758: the W4A8
    SiTU fusion path fires on A3 and crashes on its own mock).

    Rewrite the ``__device_type__`` line IN PLACE for the duration of the
    batch; the caller restores the original bytes when it finishes.  A
    PYTHONPATH shadow cannot do this job: pytest resolves each collected
    module's basedir by walking up the ``__init__.py`` chain (tests/ and
    tests/ut/ are packages) and ``sys.path.insert(0, ...)``s the repo root
    in prepend import mode — the graded tree sits AHEAD of any PYTHONPATH
    entry for every ``import vllm_ascend`` (runs 38421012498 and
    38494406932: a shadow view was active at PYTHONPATH[0] with
    PYTHONSAFEPATH=1 and W4A8 still fired).  The resolution target itself
    must say A2.

    ``_build_info.py`` is a gitignored build artifact (generated by
    setup.py at install time, never tracked), so the untouched-tree guard
    is unaffected, and the e2e lanes see the a3 profile again as soon as
    the batch restores it.  A3-specific tests patch the profile in-test
    (as upstream's do), so A3 coverage survives.  Returns
    ``(path, original_text)`` for the restore, or ``(None, None)`` when
    there is nothing to swap (kill switch MAIN2MAIN_UT_PURE_CPU=0, not a
    built tree, or no recognizable device-type line).
    """
    if os.environ.get("MAIN2MAIN_UT_PURE_CPU", "1").lower() in (
            "0", "false", "no", "off"):
        return None, None
    info = repo / "vllm_ascend" / "_build_info.py"
    try:
        original = info.read_text(encoding="utf-8")
    except OSError:
        # Not a built/installed ascend tree — grade the tree as-is rather
        # than failing the check here.
        return None, None
    if not _BUILD_INFO_DEVICE_TYPE_RE.search(original):
        ts_print(f"[pre_ci] ut: {info} has no __device_type__ line — "
                 "grading the tree's own build profile")
        return None, None
    info.write_text(
        _BUILD_INFO_DEVICE_TYPE_RE.sub(
            lambda m: m.group(1) + repr(_PURE_CPU_DEVICE_TYPE), original),
        encoding="utf-8")
    return info, original


def _restore_build_info(info: Path | None, original: str | None) -> None:
    """Put back the build profile the install generated (best effort)."""
    if info is None or original is None:
        return
    try:
        info.write_text(original, encoding="utf-8")
        ts_print("[pre_ci] ut: restored the tree's own _build_info "
                 "(e2e lanes keep their build profile)")
    except OSError as exc:
        ts_print(f"[pre_ci] ut: WARNING — could not restore {info}: {exc}")


def _build_ut_env(repo: Path, vllm_path: str | Path,
                  fake_bin_dir: Path) -> dict:
    """Env replicating CI's CPU-UT lane: pure CPU, mocked NPU, offline hub.

    The flow repo root is appended to PYTHONPATH so the ut_namespace
    plugin resolves even when the caller's environment doesn't carry it.
    The pure-CPU device profile itself is NOT an env matter — it is the
    in-tree ``_build_info`` swap in :func:`_swap_pure_cpu_build_info`.
    """
    env = os.environ.copy()
    # Production main2main runs csrc-l1-restore in the SAME job, which
    # exports these into GITHUB_ENV (prepare_csrc_l1_restore.py) — every
    # later step inherits them.  Upstream's cpu-UT lane never sees them
    # (every csrc cache step there is gated `npu_type != 'cpu'`), and
    # VLLM_ASCEND_BUILD_CACHE_DIR defeats the CMake
    # `VLLM_ASCEND_BUILD_CACHE_DIR:-${CURRENT_DIR}/build_cache` default
    # that tests/ut/_tools/test_build_cache.py asserts (run 38383045302
    # step-1).  The UT batch must judge the tree the way upstream CI
    # does, so strip the workflow's own cache plumbing from the env.
    for leaked in ("VLLM_ASCEND_BUILD_CACHE_DIR",
                   "VLLM_ASCEND_BUILD_CACHE_EVENT_LOG"):
        env.pop(leaked, None)
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
    # Upstream CI runs the pytest CONSOLE script, whose sys.path[0] is the
    # script's own directory — the repo cwd is never on sys.path.
    # `python -m pytest` instead prepends the cwd (the graded tree) ahead
    # of PYTHONPATH (run 38421012498: the venv shares the system pytest
    # and has no bin/pytest script, so the -m fallback ran).  PYTHONSAFEPATH
    # (py3.11+) gives -m the console-script sys.path semantics on both
    # invocation paths — invocation parity with upstream, whatever the
    # batch ends up importing.
    env["PYTHONSAFEPATH"] = "1"
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


_RELEASE_UT_BASELINE_FILE = Path(__file__).parent / "release_ut_baseline.json"

# Main-lane base-tree known failures, recorded once per run while the
# ascend tree is still pristine (run 38383045302: step-1 burned all 5
# attempts on 11 failures the adapter proved pre-existing — rope-cache
# batch-order state and a runner-env build-cache assertion — none
# caused by its 20-commit upstream delta).
_MAIN_UT_BASELINE_FILE = WORKSPACE_DIR / "base_ut_failures.json"


def _load_main_baseline() -> set[str]:
    """Known-failing main-lane UT node IDs of this run's base tree.

    Recorded by :func:`compute_base_ut_baseline` at kickoff, before the
    adapter touches anything.  Matching failures are reported in the
    detail and the full log but never block — the same contract as the
    release lane's static ``release_ut_baseline.json``, except the
    snapshot is per-run and per-tree (fresh mode's base = upstream main,
    whose own UT health is upstream's, not the adapter's, to fix).
    MAIN2MAIN_BASE_UT_BASELINE=0 disables the filtering (see everything).
    """
    if os.environ.get("MAIN2MAIN_BASE_UT_BASELINE", "1").lower() in (
            "0", "false", "no", "off"):
        return set()
    try:
        data = json.loads(
            _MAIN_UT_BASELINE_FILE.read_text(encoding="utf-8"))
        return set(data.get("excluded", []))
    except Exception:
        return set()


def _node_matches_baseline(node: str, baseline: set[str]) -> bool:
    """Exact node match, or the node's whole file is baselined.

    ``tests/ut/x.py::test_a`` matches key ``tests/ut/x.py::test_a`` and
    also a whole-file key ``tests/ut/x.py`` (which additionally absorbs
    the file's collection error, whose node id is the bare path).
    """
    return node in baseline or node.split("::")[0] in baseline


def _load_release_baseline() -> set[str]:
    """Known-failing release-lane UT node IDs (not adaptation-caused).

    The release-lane UT batch surfaces failures that exist on the release
    lane independent of the current diff — the same "pre-existing noise"
    that got the a2 dual-version UT deleted.  Baseline node IDs are
    reported in the detail and the full log but never block.  A whole
    test-file path (``tests/ut/x.py``, no ``::node``) is also a valid
    key — it absorbs the file's collection error on the release lane.
    MAIN2MAIN_RELEASE_UT_BASELINE=0 disables the filtering (see everything).
    """
    if os.environ.get("MAIN2MAIN_RELEASE_UT_BASELINE", "1").lower() in (
            "0", "false", "no", "off"):
        return set()
    try:
        data = json.loads(
            _RELEASE_UT_BASELINE_FILE.read_text(encoding="utf-8"))
        return set(data.get("excluded", []))
    except Exception:
        return set()


def check_ut(repo: Path, vllm_path: str | Path | None = None,
             vllm_release_path: str | Path | None = None,
             release_tag: str = "",
             known_failures: set[str] | None = None) -> dict:
    """Run the CPU-UT batch, aligned with CI's single-process execution.

    Runs the same CPU-routed tests/ut/* files as CI's CPU runner
    (linux-amd64-cpu-8-hk), but on whatever machine main2main runs on.
    Mirrors vllm-ascend's ``run_selected_tests.sh`` cpu-ut batch: ALL
    files in ONE pytest process (CI runs 2044 tests in ~44s; per-file
    subprocess isolation cost ~5 min per version).

    Runs the batch against the target main vllm checkout (``vllm_path``).
    When ``vllm_release_path`` (a worktree of the pinned release tag) and
    ``release_tag`` are also given, a SECOND batch runs against the
    release tree with ``VLLM_VERSION`` set — CI's release leg has no
    cpu-ut, so this is the only UT-level signal for the release lane.
    Release-batch node IDs listed in ``release_ut_baseline.json`` are
    reported but never block (pre-existing release-lane failures are not
    the adaptation's fault).  Main-lane node IDs matching this run's
    base-tree snapshot (``known_failures``, defaulting to the workspace
    ``base_ut_failures.json`` recorded at kickoff by
    :func:`compute_base_ut_baseline`) are reported but never block the
    same way — run 38383045302 burned 5 attempts on failures the adapter
    proved pre-existing on the fresh-mode base tree.  Pass an explicit
    ``set()`` to disable the exemption (that is exactly how the snapshot
    itself is taken).

    ``test_schedule_body_matches_pinned_release_tag`` is excluded on BOTH
    batches — see ``_BALANCE_TAG_BODY_TEST``.

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
             f"(single process, NPU-convention a2/ and a3_2/ excluded)")

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
    # Upstream CI invokes the pytest CONSOLE script (`pytest -sv ...`),
    # whose sys.path[0] is the script's own directory.  `python -m pytest`
    # instead prepends the cwd — the graded tree — AHEAD of PYTHONPATH.
    # The pure-CPU profile no longer depends on sys.path order (the
    # in-tree _build_info swap above), so this is invocation parity with
    # upstream, not a correctness knob.  Prefer the venv's pytest script;
    # fall back loudly if it is missing.
    pytest_cmd = [pytest_bin]
    if venv_dir:
        venv_pytest = Path(venv_python).parent / "pytest"
        if venv_pytest.exists():
            pytest_cmd = [str(venv_pytest)]
            ts_print(f"[pre_ci] ut: using venv pytest script {venv_pytest}")
        else:
            # No venv-local pytest (shared system site-packages): -m with
            # PYTHONSAFEPATH (see _build_ut_env) matches upstream's
            # console-script sys.path semantics.
            ts_print("[pre_ci] ut: venv has no pytest script — using "
                     f"{venv_python} -m pytest (PYTHONSAFEPATH keeps the "
                     "cwd off sys.path)")
            pytest_cmd = [str(venv_python), "-m", "pytest"]

    # Full pytest stdout is persisted here so the adapter (fix mode) can
    # grep complete tracebacks when a violation's excerpt was truncated.
    log_path = (venv_dir or WORKSPACE_DIR) / UT_FULL_LOG_NAME

    fake_bin_dir = _make_fake_npu_smi()
    # Defined before the try so the finally below can always restore, even
    # if the swap itself never ran (kill switch, missing/malformed file).
    build_info: Path | None = None
    build_info_original: str | None = None

    all_violations: list[str] = []
    baseline_violations: list[str] = []
    main_failed_nodes: list[str] = []
    all_files_clean = True
    details: list[str] = []
    # Nodeless ERROR lines are collection errors ("ERROR tests/ut/x.py -
    # ImportError: ...", no ::node): run 34583706211 died on one — with
    # parsed failures present it stayed invisible, and once alone it
    # degraded to a warnings-summary tail the adapter couldn't act on.
    failed_re = re.compile(r"^(FAILED|ERROR)\s+(\S+\.py(?:::\S+)?)")

    # Release-lane batches only: node IDs known to fail on the release
    # lane independent of the current adaptation (never block).
    baseline = _load_release_baseline() if vllm_release_path else set()
    # Main lane: node IDs this run's pristine base tree already failed
    # (see _load_main_baseline).  known_failures=None → load the snapshot;
    # an explicit set() disables the exemption (snapshot mode).
    main_baseline = (known_failures if known_failures is not None
                     else _load_main_baseline())

    # One pytest process over ALL CPU-UT files — exactly upstream's
    # cpu-UT lane shape, so no per-file list can rot as upstream's tests
    # evolve.  In pytest's collection order (which the batch now uses)
    # the known module-level polluters cannot bite their victims:
    # test_vocab_parallel_embedding.py leaks parallel_state MagicMocks
    # at module scope and test_batch_invariant.py a global
    # torch.library.Library, but both sort AFTER the ops/ files they
    # used to break (test_gdn_layerwise_kv / test_linear), and the
    # upstream-recipe control lane has been single-process green on the
    # same trees (parity runs 38416535777 / 38497619728: only W4A8, a
    # build-profile artifact, ever failed there).  The per-file
    # isolation this replaced was an os.walk-order-era guard (root
    # files ran before subdirs, polluters before victims — run
    # 38405850804's 371-violation cascade); the order fix removed its
    # reason, and a hardcoded filename list is a maintenance trap.

    # (label, vllm tree for this batch, VLLM_VERSION value or "").  The
    # release tuple is appended only when both the worktree and the tag
    # are available — otherwise the run degrades to main-only.
    versions: list[tuple[str, Path, str]] = [("main", Path(vllm_path), "")]
    if vllm_release_path and release_tag:
        rel_ver = release_tag.lstrip("v")
        versions.append((rel_ver, Path(vllm_release_path), rel_ver))

    try:
        # Swap INSIDE the try: any raise between here and the loop (or in
        # it) still reaches the finally and restores the install's bytes.
        build_info, build_info_original = _swap_pure_cpu_build_info(repo)
        if build_info is not None:
            ts_print(f"[pre_ci] ut: pure-CPU device profile active "
                     f"({build_info} __device_type__ -> "
                     f"{_PURE_CPU_DEVICE_TYPE}, upstream's SOC-less cpu-ut "
                     f"default; restored after the batch); set "
                     f"MAIN2MAIN_UT_PURE_CPU=0 to grade the tree's own "
                     f"profile")
        for label, batch_vllm, vllm_version in versions:
            env = _build_ut_env(repo, batch_vllm, fake_bin_dir)
            if vllm_version:
                # A raw git worktree has no installed vllm metadata, so
                # vllm_ascend.utils.vllm_version_is cannot infer the lane
                # from __version__ — VLLM_VERSION forces release guards to
                # resolve the way CI's release leg does.
                env["VLLM_VERSION"] = vllm_version
            ts_print(f"[pre_ci] ut: [{label}] pure-CPU env "
                     f"(ASCEND_RT_VISIBLE_DEVICES='')")

            ts_print(f"\n[pre_ci] ut: === batch [{label}] "
                     f"PYTHONPATH={env['PYTHONPATH']} ===")

            log_path.parent.mkdir(parents=True, exist_ok=True)
            env_header = (
                f"# pytest cmd: {' '.join(pytest_cmd)}\n"
                f"# repo: {repo.resolve()}\n"
                f"# vllm: {batch_vllm.resolve()}\n"
                f"# PYTHONPATH: {env['PYTHONPATH']}\n")
            if vllm_version:
                # Both lanes append to the SAME ut_full.log under labeled
                # headers — fix mode greps one file for either lane.
                with log_path.open("a", encoding="utf-8") as lf:
                    lf.write(f"\n##### [{label}] release-lane batch "
                             f"(VLLM_VERSION={vllm_version}) #####\n")
                    lf.write(env_header)
            else:
                log_path.write_text("# pre_ci UT full log\n" + env_header,
                                    encoding="utf-8")

            exclude_expr = f"not {_BALANCE_TAG_BODY_TEST}"
            if vllm_version:
                # test_vllm_version_is unit-tests the VLLM_VERSION env
                # fallback with a mocked env; the release batch sets
                # VLLM_VERSION for real, so the fallback path is
                # unreachable and the test fails for env reasons only.
                exclude_expr += " and not test_vllm_version_is"

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
                     *cpu_files, "-k", exclude_expr],
                    cwd=str(repo), capture_output=True, text=True,
                    env=env, timeout=1200,
                )
                runs.append(("batch", rr))
            except subprocess.TimeoutExpired:
                ts_print(f"[pre_ci] ut: [{label}] batch TIMEOUT(1200s)")
                all_files_clean = False
                details.append(f"{label}/batch: TIMEOUT(1200s)")

            for name, rr in runs:
                clean = strip_ansi(rr.stdout + rr.stderr)
                with log_path.open("a", encoding="utf-8") as lf:
                    lf.write(f"\n===== run: {name} (exit={rr.returncode}) =====\n")
                    lf.write(clean)
                seen: set[str] = set()
                run_blocking = 0
                for line in clean.splitlines():
                    m = failed_re.search(line.strip())
                    if m and m.group(2) not in seen:
                        seen.add(m.group(2))
                        v = f"[{label}] {line.strip()}"
                        ex = _failure_excerpt(clean, line.strip())
                        if ex:
                            v += "\n" + ex
                        if vllm_version and m.group(2) in baseline:
                            baseline_violations.append(v)
                            continue
                        if not vllm_version:
                            main_failed_nodes.append(m.group(2))
                            if _node_matches_baseline(m.group(2),
                                                      main_baseline):
                                baseline_violations.append(v)
                                continue
                        all_violations.append(v)
                        run_blocking += 1
                if rr.returncode != 0:
                    if not seen:
                        all_files_clean = False
                        all_violations.append(
                            f"[{label}] {name}: exit={rr.returncode} — "
                            f"{_unparseable_evidence(clean)}")
                    elif run_blocking:
                        all_files_clean = False
                    # else: every parsed failure baseline-matched (release
                    # lane, pre-existing) — reported above, never blocks.
                summary_m = re.search(
                    r"((?:\d+ failed, )?\d+ passed[^\n]*)", clean)
                summary = (summary_m.group(1) if summary_m
                           else f"exit={rr.returncode}")
                details.append(f"{label}/{name}: {summary}")
                ts_print(f"[pre_ci] ut: [{label}/{name}] {summary}")

        baseline_note = ""
        if baseline_violations:
            baseline_note = (f"; {len(baseline_violations)} failure(s) "
                             f"matched the known baseline (not blocking)")
            ts_print(f"[pre_ci] ut: {len(baseline_violations)} failure(s) "
                     f"matched the known baseline (not blocking)")

        failed_nodes = sorted(set(main_failed_nodes))
        if all_files_clean:
            ts_print(f"\n[pre_ci] ut: OK — all {len(cpu_files)} files clean")
            return {"violations": [],
                    "detail": f"UT clean ({len(cpu_files)} files, "
                              f"single-process batch)" + baseline_note,
                    "log_path": str(log_path),
                    "venv_python": str(venv_python) if venv_dir else "",
                    "failed_nodes": failed_nodes}
        ts_print(f"\n[pre_ci] ut: {len(all_violations)} failure(s):")
        for v in all_violations[:20]:
            ts_print(f"  {v}")
        if len(all_violations) > 20:
            ts_print(f"  ... and {len(all_violations) - 20} more")
        return {"violations": all_violations,
                "detail": f"{len(all_violations)} UT failure(s): "
                          + "; ".join(details) + baseline_note,
                "log_path": str(log_path),
                "venv_python": str(venv_python) if venv_dir else "",
                "failed_nodes": failed_nodes}
    finally:
        # The venv persists across attempts/steps (recreated only on numpy
        # spec mismatch) — only the throwaway fake-bin dir is deleted here.
        if fake_bin_dir.exists():
            shutil.rmtree(fake_bin_dir, ignore_errors=True)
        # Restore the build profile the install generated, whatever the
        # batch outcome — the e2e lanes grade the same tree afterwards.
        _restore_build_info(build_info, build_info_original)


def compute_base_ut_baseline(repo: Path, vllm_path: str | Path) -> dict | None:
    """Snapshot the base tree's known-failing main-lane UT node IDs.

    Called by the flow ONCE per run, at kickoff, while the vllm tree is
    still pristine at the base commit (before any pointer bump).  Runs the
    same batch check_ut runs, but with the exemption disabled
    (``known_failures=set()``) so every real failure is captured, then
    writes :data:`_MAIN_UT_BASELINE_FILE` so later ``check_ut`` calls in
    this run treat those node IDs (or their whole file) as non-blocking.

    Returns the written record, or None on any failure (missing vllm
    checkout, empty repo) — a failed snapshot simply means the run has no
    baseline and the gate stays as strict as before.  Set
    ``MAIN2MAIN_BASE_UT_BASELINE=0`` to skip entirely.
    """
    if os.environ.get("MAIN2MAIN_BASE_UT_BASELINE", "1").lower() in (
            "0", "false", "no", "off"):
        ts_print("[pre_ci] ut: base-tree baseline disabled by env, skipping")
        return None
    vllm_path = Path(vllm_path)
    if not (vllm_path / ".git").exists():
        ts_print(f"[pre_ci] ut: no vllm checkout at {vllm_path}, "
                 f"base-tree baseline skipped")
        return None
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(vllm_path),
            capture_output=True, text=True, timeout=30,
        )
        recorded_head = head.stdout.strip() if head.returncode == 0 else ""
        ts_print(f"[pre_ci] ut: snapshotting base-tree UT baseline "
                 f"@ {recorded_head[:12] or 'unknown'} ...")
        result = check_ut(repo, vllm_path=vllm_path, known_failures=set())
    except Exception as exc:  # noqa: BLE001 — snapshot must never kill a run
        ts_print(f"[pre_ci] ut: base-tree baseline snapshot failed: {exc}")
        return None
    failed_nodes = sorted(set(result.get("failed_nodes", [])))
    if result.get("skipped"):
        # Nothing ran (broken venv, no pytest) — a written record would
        # falsely certify the base tree as clean.
        ts_print("[pre_ci] ut: base-tree baseline snapshot skipped "
                 f"({result.get('detail', '')}), no record written")
        return None
    record = {
        "excluded": failed_nodes,
        "recorded_head": recorded_head,
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "detail": result.get("detail", ""),
    }
    try:
        _MAIN_UT_BASELINE_FILE.write_text(
            json.dumps(record, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        ts_print(f"[pre_ci] ut: failed to write base-tree baseline: {exc}")
        return None
    ts_print(f"[pre_ci] ut: base-tree baseline written "
             f"({len(failed_nodes)} node(s)) -> {_MAIN_UT_BASELINE_FILE}")
    return record
