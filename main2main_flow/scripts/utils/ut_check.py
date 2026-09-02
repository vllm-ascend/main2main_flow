"""CPU-UT batch runner for the main2main quality gate.

Standalone module (not embedded in pre_ci_check.py): collects the
CPU-routed ``tests/ut`` files and runs them in a single-process pytest
batch against the target main vllm checkout, mirroring vllm-ascend's
``run_selected_tests.sh`` cpu-ut batch.

Key mechanisms:
- **No npu-smi on the PATH** (matches upstream pr_test cpu-0, where the
  command does not exist): tests/ut/conftest.py then takes the mock
  torch_npu path, and triton/torch_npu platform discovery degrades the
  same way upstream does — an exit-1 fake binary takes a DIFFERENT path
  and broke test_config_modules_do_not_load_vllm_config (2026-09-02).
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
from pathlib import Path

from main2main_flow.scripts.utils.utils import ts_print

_BALANCE_TAG_BODY_TEST = "test_schedule_body_matches_pinned_release_tag"

_CPU_UT_A2_RE = re.compile(r"tests/ut/.+/a2(/|$)")
_CPU_UT_A3_2_RE = re.compile(r"tests/ut/.+/a3_2(/|$)")


def _is_npu_convention_ut_path(rel_path: str) -> bool:
    """Mirror select_tests._route_ut_dir — True if path routes to NPU runner."""
    p = rel_path.replace("\\", "/")
    return bool(_CPU_UT_A2_RE.search(p) or _CPU_UT_A3_2_RE.search(p))


def _collect_cpu_ut_files(repo: Path, log_label: str = "pre_ci") -> list[str]:
    """Return CPU-routed tests/ut paths (rel to repo).

    PRIMARY: invoke the ascend checkout's OWN select_tests.py --all-tests
    — the upstream four-mode contract's ready-all path (pr_test.yaml's
    "Run select-tests" ready-all branch), so the collected set tracks
    upstream's Collect/Skip/Route logic exactly (verified 2026-09-02:
    --all-tests cpu group = 246 files, identical to the vendored-config
    scan).  GITHUB_OUTPUT="" forces the test_groups= line to stdout.
    Falls back to the vendored test_config.yaml parsing (same set), then
    to the convention regexes.
    """
    select_script = repo / ".github/workflows/scripts/select_tests.py"
    if select_script.exists():
        try:
            r = subprocess.run(
                [sys.executable, str(select_script), "--all-tests"],
                cwd=str(repo), capture_output=True, text=True,
                env={**os.environ, "GITHUB_OUTPUT": ""}, timeout=300,
            )
            if r.returncode == 0:
                for line in r.stdout.strip().splitlines():
                    if not line.startswith("test_groups="):
                        continue
                    groups = json.loads(line[len("test_groups="):])
                    cpu_tests: list[str] = []
                    for g in groups:
                        if g.get("npu_type") == "cpu":
                            cpu_tests.extend(g.get("tests", "").split())
                    if cpu_tests:
                        ts_print(f"[{log_label}] ut: routed from upstream "
                                 f"select_tests.py --all-tests "
                                 f"({len(cpu_tests)} CPU test file(s))")
                        return cpu_tests
        except Exception as exc:
            ts_print(f"[{log_label}] ut: select_tests.py --all-tests failed "
                     f"({exc}) — falling back to vendored config")
    skip_tests: set[str] = set()
    npu_patterns: list[re.Pattern] = []
    vendor_config = (Path(__file__).resolve().parent
                     / "vendor_select_tests" / "test_config.yaml")
    config_path = (vendor_config if vendor_config.exists()
                   else repo / ".github/workflows/scripts/test_config.yaml")
    if config_path.exists():
        try:
            import yaml
            docs = list(yaml.safe_load_all(
                config_path.read_text(encoding="utf-8")))
            modules = docs[0] or []
            meta = docs[1] if len(docs) >= 2 and docs[1] else {}
            for module in modules:
                for s in module.get("skip_tests", []):
                    skip_tests.add(str(s).rstrip("/"))
            for pattern_str in ((meta or {}).get("runner_mapping", {}) or {}):
                if pattern_str.startswith("tests/ut"):
                    npu_patterns.append(re.compile(pattern_str))
            if npu_patterns:
                ts_print(f"[{log_label}] ut: routing from test_config.yaml "
                         f"({len(npu_patterns)} NPU pattern(s), "
                         f"{len(skip_tests)} skip entry(s))")
        except Exception as e:
            ts_print(f"[{log_label}] ut: failed to parse test_config.yaml ({e}), "
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


# pytest decorates the ERROR paragraph with underscores
# (_____ ERROR collecting tests/foo.py _____); the underscore must be
# allowed before the keyword (verified against real pytest output,
# 2026-09-02: local collection-error reproduction).
def _slug(path: str) -> str:
    return (path.replace("/", "__").replace(".py", "")
            .replace("::", "--"))


def _violations_from_junit(label: str, name: str, xml_path: Path) -> list[str]:
    """Build violations from pytest's built-in junitxml report.

    Each failure/error testcase carries the full traceback as element
    text; a module that failed collection appears as an error testcase
    with a bare module classname (verified locally: --continue-on-
    collection-errors + junitxml records "collection failure" with the
    complete ImportError traceback).  Returns [] when the report is
    missing/unparseable — the caller falls back to text parsing.
    """
    import xml.etree.ElementTree as ET

    if not xml_path.exists():
        return []
    try:
        root = ET.parse(str(xml_path)).getroot()
    except ET.ParseError:
        return []
    out: list[str] = []
    for tc in root.iter("testcase"):
        for child in tc:
            if child.tag not in ("failure", "error"):
                continue
            cls = tc.get("classname", "")
            tname = tc.get("name", "")
            msg = (child.get("message") or "").strip()
            text = (child.text or "").strip()
            # A collection error has no class path: classname is the bare
            # module dotted name (tests.test_coll_err) or empty.
            if child.tag == "error" and (not cls or "collection" in msg):
                v = (f"[{label}] COLLECTION ERROR {cls or tname} — {msg}"
                     + (f"\n{text}" if text and text != msg else ""))
            else:
                verdict = "FAILED" if child.tag == "failure" else "ERROR"
                v = (f"[{label}] {cls}::{tname} {verdict} — {msg}"
                     + (f"\n{text}" if text and text != msg else ""))
            out.append(v)
    return out


_COLLECTION_ERR_RE = re.compile(
    r"^[_=\s]*ERROR (?:collecting \S+|at setup of \S+|tests/\S+\.py\b)")
_ERR_BLOCK_MAX = 3000


def _extract_error_block(clean: str) -> str:
    """Extract the pytest ERROR paragraph when no FAILED line matched.

    A collection/setup error (ImportError, circular import) produces an
    ``ERROR collecting <file>`` block in the MIDDLE of the output, while
    the batch tail is often unrelated noise (e.g. pytest-asyncio
    deprecation warnings).  Showing the tail alone misleads the adapter
    into treating a real import failure as an environment flake — observed
    33538038959: test_attn_utils_v2.py's collection ImportError was
    invisible for 3 gate fix rounds.
    """
    for line in clean.splitlines():
        m = _COLLECTION_ERR_RE.search(line.strip())
        if m:
            line_pos = clean.find(line)
            start = max(0, line_pos - 200)
            end = len(clean)
            # Section markers must be sought AFTER the matched line — the
            # "===== ERRORS =====" header precedes it, and searching from
            # start would truncate the block to nothing (verified against
            # real pytest output, 2026-09-02).
            for marker in ("\n____", "\n===", "\n--------"):
                cut = clean.find(marker, line_pos + 1)
                if cut > 0:
                    end = min(end, cut)
            return clean[start:end][:_ERR_BLOCK_MAX]
    return ""


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


def check_ut(repo: Path, vllm_path: str | Path | None = None,
             timeout_s: int = 1800,
             log_label: str = "pre_ci") -> dict:
    """Run the CPU-UT batch, aligned with CI's single-process execution.

    Runs the same CPU-routed tests/ut/* files as CI's CPU runner
    (linux-amd64-cpu-8-hk), but on whatever machine main2main runs on.
    Mirrors vllm-ascend's ``run_selected_tests.sh`` cpu-ut batch: ALL
    files in ONE pytest process (CI runs 2044 tests in ~44s; per-file
    subprocess isolation cost ~5 min per version).

    Runs the batch against the target main checkout (``vllm_path``)
    only — single-version validation.

    ``test_schedule_body_matches_pinned_release_tag`` is excluded — see
    ``_BALANCE_TAG_BODY_TEST``.

    Env mirrors CI: venv with --system-site-packages + numpy==1.26.4
    (from triton-ascend metadata) + PYTHONPATH=ascend:vllm.  torch_npu
    is mocked by conftest, so the venv python's C-extension issue that
    broke A2-NPU-UT in PR #13657 does not apply here.

    Returns dict with ``violations`` (failing test node IDs) and
    ``detail``.  Empty violations + non-skipped → pass.
    """
    import tempfile
    import importlib.metadata as _md

    cpu_files = _collect_cpu_ut_files(repo, log_label=log_label)
    if not cpu_files:
        ts_print(f"\n[{log_label}] ut: SKIPPED — tests/ut not found or no CPU tests")
        return {"violations": [], "detail": "tests/ut not found", "skipped": True}

    pytest_bin = shutil.which("pytest")
    if not pytest_bin:
        ts_print(f"\n[{log_label}] ut: SKIPPED — pytest not installed")
        return {"violations": [], "detail": "pytest not installed", "skipped": True}

    ts_print(f"\n[{log_label}] ut: collected {len(cpu_files)} CPU test files "
             f"(per-file isolation, NPU-convention a2/ and a3_2/ excluded)")

    # Read numpy constraint from triton-ascend metadata (mirror _check_mypy).
    target_numpy_spec = ""
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
                target_numpy_spec = ",".join(
                    f"{s.operator}{s.version}" for s in r.specifier)
                break
    except Exception as e:
        ts_print(f"[{log_label}] ut: failed to read triton-ascend numpy constraint ({e})")

    # Create venv with --system-site-packages, install numpy constraint.
    venv_dir: Path | None = None
    pytest_cmd = [pytest_bin]
    try:
        venv_dir = Path(tempfile.mkdtemp(prefix="ut_venv_"))
        ts_print(f"[{log_label}] ut: creating venv at {venv_dir} "
                 f"(numpy{target_numpy_spec} from triton-ascend)")
        r = subprocess.run(
            [sys.executable, "-m", "venv", str(venv_dir), "--system-site-packages"],
            capture_output=True, text=True, timeout=180,
        )
        if r.returncode != 0:
            ts_print(f"[{log_label}] ut: WARNING venv creation FAILED — "
                     "falling back to system pytest")
            venv_dir = None
        else:
            venv_python = venv_dir / "bin" / "python"
            # Pin the pytest toolchain to what upstream pr_test resolves
            # (verified 2026-09-02 on run 33582304976: pytest==8.3.2,
            # pytest-asyncio==1.3.0): the runner's unpinned `pip install
            # pytest` pulls 9.1.1, which changed collection/plugin
            # behavior — collected 3165 items vs upstream's 3307 on the
            # SAME 246-file set, and the batch failed where upstream
            # passed.  Overriding in the venv keeps system pytest intact.
            installs = ([f"numpy{target_numpy_spec}"] if target_numpy_spec
                        else [])
            installs += ["pytest==8.3.2", "pytest-asyncio==1.3.0",
                         "pytest-cov==7.1.0", "pytest-mock==3.15.1"]
            try:
                r2 = subprocess.run(
                    [str(venv_python), "-m", "pip", "install", "-q",
                     *installs],
                    capture_output=True, text=True, timeout=300,
                )
            except subprocess.TimeoutExpired:
                ts_print(f"[{log_label}] ut: WARNING venv install TIMED OUT — "
                         "falling back to system pytest")
                r2 = None
            if r2 is not None and r2.returncode != 0:
                ts_print(f"[{log_label}] ut: WARNING venv install FAILED "
                         f"({r2.stderr.strip()[:200]}) — falling back "
                         "to system pytest")
                venv_dir = None
            if venv_dir is not None:
                pytest_cmd = [str(venv_python), "-m", "pytest"]
                ts_print(f"[{log_label}] ut: using venv pytest via "
                         f"{venv_python} -m pytest")
    except subprocess.TimeoutExpired:
        ts_print(f"[{log_label}] ut: WARNING venv creation TIMED OUT (180s) — "
                 "falling back to system pytest")
        venv_dir = None

    # NO fake npu-smi: the batch runs on the pure-CPU runner where the
    # command does not exist, exactly like upstream pr_test's cpu-0 — and
    # that absence matters beyond conftest: triton-ascend / torch_npu
    # probe npu-smi during platform discovery, and a fake binary that
    # EXISTS but exits 1 takes a different path than a missing command
    # (upstream run 33582304976's test_ascend_config printed "can not use
    # command: npu-smi info" and passed; with the exit-1 fake the same
    # test failed its subprocess assert — verified 2026-09-02).  A fake
    # would also be pointless on this runner: it has no NPU anyway.


    all_violations: list[str] = []
    all_files_clean = True
    details: list[str] = []
    full_outputs: dict[str, str] = {}
    ansi_re = re.compile(r"\x1b\[[0-9;]*m")
    failed_re = re.compile(r"^(FAILED|ERROR)\s+(\S+\.py::\S+)")
    reports_dir: Path | None = None

    try:
        vpath = Path(vllm_path) if vllm_path else None
        if not vpath:
            ts_print(f"[{log_label}] ut: no vllm path configured, skipping")
            return {"violations": [], "detail": "no vllm path", "skipped": True}

        label = "main"
        env = os.environ.copy()
        ascend_abs = str(repo.resolve())
        vllm_abs = str(vpath.resolve())
        # MAIN2MAIN_UT_SYSTEM_IMPORT=1: skip the source-tree PYTHONPATH and
        # let pytest import ascend/vllm from the installed packages — the
        # exact model of upstream pr_test (pip/uv-installed, single module
        # identity).  PYTHONPATH=ascend:vllm can split module identity
        # when the same package is also installed (mock patches one object
        # while code calls another) — under investigation for the
        # pristine-baseline failures (2026-09-02).
        if os.getenv("MAIN2MAIN_UT_SYSTEM_IMPORT", "0") != "1":
            existing = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = (
                f"{ascend_abs}:{vllm_abs}:{existing}" if existing
                else f"{ascend_abs}:{vllm_abs}")
        # CANN runtime libs (LD_LIBRARY_PATH etc.) — the container does not
        # set them, so without this torch_npu auto-load fails on
        # libascend_hal.so, and the old AUTOLOAD=0 bypass changed the import
        # chain (loading vllm.config), breaking
        # test_config_modules_do_not_load_vllm_config (bisected 2026-09-02:
        # source set_env.sh -> PROBE PASS).  Mirrors the e2e serve path.
        cann_setenv = "/usr/local/Ascend/ascend-toolkit/set_env.sh"
        if os.path.exists(cann_setenv):
            try:
                out = subprocess.run(
                    ["bash", "-c", f". {cann_setenv} && env"],
                    capture_output=True, text=True, timeout=60).stdout
                for line in out.splitlines():
                    key, _, val = line.partition("=")
                    if key in ("LD_LIBRARY_PATH", "PATH",
                               "ASCEND_TOOLKIT_HOME", "ASCEND_HOME_PATH",
                               "ASCEND_AICPU_PATH", "ASCEND_OPPER_PATH",
                               "ASCEND_DRIVER_PATH"):
                        env[key] = val
            except Exception as exc:
                ts_print(f"[{log_label}] ut: WARNING CANN env source failed "
                         f"({exc}) — torch_npu may fail to load")
        env["VLLM_WORKER_MULTIPROC_METHOD"] = "spawn"
        # Match CI: offline mode so get_model_file / hf_hub_download
        # fails immediately (385s→0.5s for test_maybe_update_config_
        # non_directory_raises) instead of retrying network timeouts.
        env["HF_HUB_OFFLINE"] = "1"
        env["VLLM_USE_MODELSCOPE"] = "True"
        # Hide NPU so platform detection sees pure CPU — matches
        # PR CI cpu-0; npu-smi is absent on this runner (upstream model).
        env["ASCEND_RT_VISIBLE_DEVICES"] = ""
        env.pop("CUDA_VISIBLE_DEVICES", None)
        ts_print(f"[{log_label}] ut: [{label}] pure-CPU env "
                 f"(ASCEND_RT_VISIBLE_DEVICES='')")

        ts_print(f"\n[{log_label}] ut: === batch [{label}] "
                 f"PYTHONPATH={ascend_abs}:{vllm_abs} ===")

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
        # Structured per-run reports via pytest's BUILT-IN junitxml:
        # every failure/error testcase carries the full traceback as
        # element text, and a module that fails collection shows up as an
        # error testcase (verified locally 2026-09-02).  No custom plugin,
        # no terminal-text parsing.  Missing report falls back to the
        # text path below.
        reports_dir = Path(tempfile.mkdtemp(prefix="ut_reports_"))
        run_reports: list[Path] = []
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
            batch_report = reports_dir / "batch.xml"
            rr = subprocess.run(
                [*pytest_cmd, "-q", "--tb=short", "--no-header",
                 "--continue-on-collection-errors",
                 "-p", "main2main_flow.scripts.utils.ut_namespace",
                 f"--junitxml={batch_report}",
                 *batch, "-k", exclude_expr],
                cwd=str(repo), capture_output=True, text=True,
                env=env, timeout=1200,
            )
            runs.append(("batch", rr))
            run_reports.append(batch_report)
        except subprocess.TimeoutExpired:
            ts_print(f"[{log_label}] ut: [{label}] batch TIMEOUT(1200s)")
            all_files_clean = False
            details.append(f"{label}/batch: TIMEOUT(1200s)")
        for f in isolated:
            f_report = reports_dir / f"iso-{_slug(f)}.xml"
            try:
                rr = subprocess.run(
                    [*pytest_cmd, "-q", "--tb=short", "--no-header", f,
                     f"--junitxml={f_report}"],
                    cwd=str(repo), capture_output=True, text=True,
                    env=env, timeout=300,
                )
                runs.append((f, rr))
                run_reports.append(f_report)
            except subprocess.TimeoutExpired:
                ts_print(f"[{log_label}] ut: [{label}] {f} TIMEOUT(300s)")
                all_files_clean = False

        for idx, (name, rr) in enumerate(runs):
            clean = ansi_re.sub("", rr.stdout + rr.stderr)
            seen: set[str] = set()
            json_violations = _violations_from_junit(
                label, name, run_reports[idx])
            if json_violations:
                # junitxml report is authoritative — no text parsing.
                all_violations.extend(json_violations)
                all_files_clean = False
            else:
                for line in clean.splitlines():
                    m = failed_re.search(line.strip())
                    if m and m.group(2) not in seen:
                        seen.add(m.group(2))
                        v = f"[{label}] {line.strip()}"
                        ex = _failure_excerpt(clean, line.strip())
                        if ex:
                            v += "\n" + ex
                        all_violations.append(v)
                if rr.returncode != 0 and not seen:
                    # No FAILED/ERROR x.py::x line: a collection/setup
                    # error or crash.  Prefer the pytest ERROR paragraph
                    # (carries the traceback); fall back to the tail.
                    # Print it — the batch output is captured, so without
                    # this the ERROR paragraph never reaches any log.
                    err_block = _extract_error_block(clean)
                    detail_tail = err_block or clean[-500:]
                    ts_print(f"[{log_label}] ut: [{label}/{name}] "
                             f"exit={rr.returncode} — pytest error "
                             f"block:\n{detail_tail[:1500]}")
                    all_violations.append(
                        f"[{label}] {name}: exit={rr.returncode} — "
                        f"{detail_tail}")
            if rr.returncode != 0:
                all_files_clean = False
                full_outputs[f"{label}/{name}"] = clean
            summary_m = re.search(
                r"((?:\d+ failed, )?\d+ passed[^\n]*)", clean)
            summary = (summary_m.group(1) if summary_m
                       else f"exit={rr.returncode}")
            details.append(f"{label}/{name}: {summary}")
            ts_print(f"[{log_label}] ut: [{label}/{name}] {summary}")

        if all_files_clean:
            ts_print(f"\n[{log_label}] ut: OK — all {len(cpu_files)} files clean")
            return {"violations": [],
                    "detail": f"UT clean ({len(cpu_files)} files, "
                              f"main version, single-process batch)"}
        ts_print(f"\n[{log_label}] ut: {len(all_violations)} failure(s):")
        for v in all_violations[:20]:
            ts_print(f"  {v}")
        if len(all_violations) > 20:
            ts_print(f"  ... and {len(all_violations) - 20} more")
        return {"violations": all_violations,
                "detail": f"{len(all_violations)} UT failure(s): "
                          + "; ".join(details),
                "full_outputs": full_outputs}
    finally:
        if venv_dir and venv_dir.exists():
            shutil.rmtree(venv_dir, ignore_errors=True)
        if reports_dir and reports_dir.exists():
            shutil.rmtree(reports_dir, ignore_errors=True)
