# SPDX-License-Identifier: Apache-2.0
"""Flow-owned, pre-adaptation scan of committed source snapshots.

Unlike the standalone clean-checkout CLI, kickoff may run from an installed
wheel and its inputs may contain ignored build output. Read committed snapshots
without switching either live checkout or importing vLLM/device libraries.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from main2main_flow.scripts.utils.utils import (
    INTERFACE_METADATA_FILE,
    INTERFACE_QA_REPORT_FILE,
    INTERFACE_RUNTIME_CACHE_DIR,
    INTERFACE_STATUS_FILE,
    ts_print,
)

from .run import _scan_prepared, cache_key, clean_head, git, json_write, require_sha


def _package_fingerprint() -> str:
    """Identify the installed engine and producer, including editable sources."""
    package = Path(__file__).resolve().parents[1]
    files = [package / "scripts/utils/utils.py"]
    for directory in ("interface_contracts", "interface_report"):
        files.extend((package / directory).rglob("*.py"))
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(path.relative_to(package).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def _tracked_snapshot(root: Path) -> tuple[str, str]:
    # initialize chmods a helper; file-mode-only changes do not alter Python
    # source. Tracked content changes, including staged ones, must be committed.
    head = git(root, "rev-parse", "HEAD")
    changes = git(root, "-c", "core.fileMode=false", "diff", "HEAD", "--name-only")
    if changes:
        raise ValueError(f"Commit tracked source changes before detection: {root}\n{changes}")
    return head, git(root, "status", "--porcelain", "--untracked-files=no")


def _clone_snapshot(source: Path, target: Path, sha: str) -> None:
    subprocess.run(
        ["git", "clone", "--shared", "--no-checkout", "--quiet", "--", str(source), str(target)],
        check=True,
        capture_output=True,
        text=True,
        timeout=120,
    )
    git(target, "checkout", "--quiet", "--detach", sha)
    clean_head(target, sha)


def scan_for_flow(
    vllm_root: Path,
    ascend_root: Path,
    old: str,
    new: str,
    output: Path,
    *,
    cache_dir: Path | None = None,
) -> Path:
    """Scan detect()'s exact range once before planning; return one QA Markdown.

    Findings are advisory. Input/engine/report failures propagate to kickoff
    before any adapter call; a failed scan cannot silently become a clean run.
    """
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    report_path = output / INTERFACE_QA_REPORT_FILE
    report_path.unlink(missing_ok=True)
    json_write(output / INTERFACE_STATUS_FILE, {"scan": "pending", "qa": "disabled"})
    try:
        vllm_root, ascend_root = vllm_root.resolve(), ascend_root.resolve()
        for root in (vllm_root, ascend_root):
            if output == root or root in output.parents:
                raise ValueError("Interface output must be outside vLLM/Ascend source checkouts")
        old, new = require_sha(old), require_sha(new)
        sources = {root: _tracked_snapshot(root) for root in (vllm_root, ascend_root)}
        ascend_sha = sources[ascend_root][0]
        git(vllm_root, "merge-base", "--is-ancestor", old, new)
        fingerprint = _package_fingerprint()
        inputs = {
            "schema_version": 1,
            "engine_sha": fingerprint,
            "python": f"{sys.version_info.major}.{sys.version_info.minor}",
            "vllm_old_sha": old,
            "vllm_new_sha": new,
            "vllm_ascend_sha": ascend_sha,
            "scenario": "main2main",
            "profile": "exact-contracts",
            "external_sources": {},
            "index_workers": 1,
            "analysis_workers": 1,
        }
        key = cache_key(inputs)
        cache_root = cache_dir or (
            Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / INTERFACE_RUNTIME_CACHE_DIR
        )
        cache = cache_root.resolve() / key
        for root in (vllm_root, ascend_root):
            if cache == root or root in cache.parents:
                raise ValueError("Interface cache must be outside vLLM/Ascend source checkouts")
        with tempfile.TemporaryDirectory(prefix="main2main-interface-") as temporary:
            snapshots = Path(temporary)
            up, down = snapshots / "vllm", snapshots / "ascend"
            _clone_snapshot(vllm_root, up, new)
            _clone_snapshot(ascend_root, down, ascend_sha)
            # Use the same marker/conf.py fallback as detect(), now on the
            # committed snapshot so a generated local marker cannot set a range.
            from main2main_flow.scripts.utils.detect_commits import _extract_from_conf_py

            if _extract_from_conf_py(down)["base_commit"] != old:
                raise ValueError("Resolved old SHA does not match the committed Ascend baseline")

            def validate_sources() -> None:
                clean_head(up, new)
                clean_head(down, ascend_sha)
                if _package_fingerprint() != fingerprint:
                    raise ValueError("Installed interface engine changed during detection")
                for root, expected in sources.items():
                    if _tracked_snapshot(root) != expected:
                        raise ValueError(f"Live source changed during detection: {root}")

            metadata = {
                "inputs": inputs,
                "cache_key": key,
                "roots": {
                    "engine_root": str(Path(__file__).resolve().parents[2]),
                    "vllm_root": str(up),
                    "ascend_root": str(down),
                },
                "live_sources": {"vllm": str(vllm_root), "ascend": str(ascend_root)},
                # The caller can supply a locally rebased cumulative baseline.
                # Do not invent a public GitHub blob link for that commit.
                "resolution": {"range_source": "flow_detect", "source_mode": "incremental_rebased"},
                "qa": {"status": "disabled", "model_calls": 0},
            }
            json_write(output / INTERFACE_METADATA_FILE, metadata)
            ts_print(f"[interface-scan] {old[:12]} -> {new[:12]}, Ascend {ascend_sha[:12]}")
            args = argparse.Namespace(
                output=output,
                cache_dir=cache,
                force_rescan=False,
                timeout_seconds=2400,
                github_output=None,
            )
            _scan_prepared(args, metadata, validate_sources, [up, down])
            validate_sources()
            if not report_path.is_file() or report_path.stat().st_size > 1_000_000:
                raise ValueError("QA interface Markdown missing or exceeds 1 MB")
            if not report_path.read_text(encoding="utf-8").strip():
                raise ValueError("QA interface Markdown is empty")
        return report_path
    except BaseException as error:
        json_write(output / INTERFACE_STATUS_FILE, {"scan": "failed", "qa": "disabled", "error": str(error)})
        report_path.unlink(missing_ok=True)
        raise
