#!/usr/bin/env python3
"""Detect base and target vLLM commits for the main2main upgrade pipeline.

Data sources:
  - base_commit:   vllm-ascend/.github/vllm-main-verified.commit (new format),
                   falling back to the hardcoded "main_vllm_commit" field in
                   vllm-ascend/docs/source/conf.py (old format)
  - compat_tag:    extracted from conf.py ("main_vllm_tag")
  - target_commit: HEAD of the local vLLM repository (unless overridden)
"""
from __future__ import annotations

import re
from pathlib import Path

from main2main_flow.utils import run_git

COMMIT_RE = re.compile(r"^[0-9a-f]{40}$")


def _validate_commit(source: Path, value: str) -> str:
    value = value.strip().lower()
    if not COMMIT_RE.fullmatch(value):
        raise RuntimeError(
            f"invalid vLLM commit in {source}: "
            f"expected a 40-char hex SHA, got {value[:80]!r}"
        )
    return value


def _extract_from_conf_py(ascend_path: Path) -> dict[str, str | None]:
    """Parse the pinned vLLM commit and compatibility tag.

    Tries the verified-commit file first (new format), then falls back to
    the hardcoded SHA in conf.py (old format).
    """
    verified_path = ascend_path / ".github" / "vllm-main-verified.commit"
    conf_path = ascend_path / "docs" / "source" / "conf.py"
    if verified_path.exists():
        base_commit = _validate_commit(
            verified_path, verified_path.read_text(encoding="utf-8")
        )
    else:
        if not conf_path.exists():
            raise RuntimeError(f"Error: {conf_path} not found")
        conf_text = conf_path.read_text(encoding="utf-8")
        commit_match = re.search(r'"main_vllm_commit":\s*"([0-9a-f]{40})"', conf_text)
        if not commit_match:
            raise RuntimeError("Error: could not find main_vllm_commit in conf.py")
        base_commit = _validate_commit(conf_path, commit_match.group(1))

    compat_tag = None
    if conf_path.exists():
        tag_match = re.search(
            r'"main_vllm_tag":\s*"([^"]+)"', conf_path.read_text(encoding="utf-8")
        )
        compat_tag = tag_match.group(1) if tag_match else None
    return {
        "base_commit": base_commit,
        "compat_tag": compat_tag,
    }


def _get_repo_head(repo_path: Path) -> str:
    """Return the HEAD commit SHA of a local git repository."""
    if not repo_path.exists():
        raise RuntimeError(f"Error: path does not exist: {repo_path}")

    return run_git(repo_path, "rev-parse", "HEAD").strip()


def detect(
    vllm_path: Path,
    ascend_path: Path,
    target_commit: str | None = None,
) -> tuple[dict, bool]:
    """Run drift detection.

    Returns a tuple ``(result, has_commit)`` where ``result`` is the detect
    dict (base_commit, target_commit, compat_tag) and ``has_commit`` is True
    when base and target differ.
    """
    conf = _extract_from_conf_py(ascend_path)
    target = target_commit if target_commit else _get_repo_head(vllm_path)

    result = {
        "base_commit": conf["base_commit"],
        "target_commit": target,
        "compat_tag": conf["compat_tag"],
    }

    return result, conf["base_commit"] != target
