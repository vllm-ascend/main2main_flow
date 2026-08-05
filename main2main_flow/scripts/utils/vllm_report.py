"""vllm-report knowledge base query module.

Loads adaptation knowledge from a local vllm-report checkout (cloned by
flow.initialize to workspace/repos/vllm-report) and returns a markdown
snippet for the adapter prompt.

Three-way query per step:
  1. commit-level: scan data/vllm/analysis/*.json for the step's end_commit,
     extract ascend_impact.functionality + deep_analysis.adaptation_guide
  2. path-level: match changed_files against patch_impact_map +
     impact_judgment_rules.definitely_affected_paths in architecture.json
  3. interface-level: match changed_files against interface_surface
     inheritable_interfaces, extract ascend_impl + key_methods

All failures are non-fatal - returns empty string so flow degrades to
the existing grep-based code exploration.
"""
from __future__ import annotations

import json
from pathlib import Path

from main2main_flow.scripts.utils.utils import ts_print


def _find_commit_analysis(report_dir: Path, target_commit: str) -> dict | None:
    """Scan data/vllm/analysis/*.json for a commit matching target_commit.

    target_commit may be short SHA - match by prefix.
    """
    analysis_dir = report_dir / "data" / "vllm" / "analysis"
    if not analysis_dir.is_dir():
        return None
    target = target_commit.lower()
    for json_file in sorted(analysis_dir.glob("*.json"), reverse=True):
        try:
            data = json.loads(json_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for commit in data.get("commits", []):
            sha = commit.get("sha", "").lower()
            if sha and (sha.startswith(target) or target.startswith(sha)):
                return commit
    return None


def _format_commit_section(commit: dict) -> list[str]:
    """Format commit-level analysis as markdown lines."""
    lines: list[str] = []
    ai = commit.get("ascend_impact") or {}
    if ai.get("ascend_affected"):
        lines.append(f"- ascend_impact: {ai.get('functionality', '').strip()}")
        testing = (ai.get("testing") or "").strip()
        if testing and testing != "无影响":
            lines.append(f"- testing: {testing}")
        if ai.get("needs_test_update"):
            areas = ai.get("suggested_test_areas") or []
            if areas:
                lines.append(f"- needs_test_update: {', '.join(areas)}")
        da = commit.get("deep_analysis") or {}
        if da:
            guide = (da.get("adaptation_guide") or "").strip()
            if guide:
                lines.append("- adaptation_guide (with line numbers):")
                for gl in guide.splitlines():
                    lines.append(f"  > {gl}")
            ifs = da.get("affected_interfaces") or []
            if ifs:
                lines.append(f"- affected_interfaces: {', '.join(ifs)}")
            effort = da.get("adaptation_effort")
            if effort:
                lines.append(f"- adaptation_effort: {effort}")
    else:
        lines.append(f"- ascend_impact: {ai.get('functionality', 'no ascend impact').strip()}")
    return lines


def _match_path(key: str, changed_files: list[str]) -> bool:
    """Check if a patch_impact_map key or path matches any changed file.

    Keys are fuzzy (e.g. 'vllm/compilation/' or 'vllm/v1/worker/gpu/model_runner.py
    (GPUModelRunner)'). Match if the changed file path contains or is contained
    by the key's path prefix.
    """
    # Extract the leading path from the key (before any ' - ' or '(')
    import re
    path_match = re.match(r'^([^\s(]+)', key)
    if not path_match:
        return False
    key_path = path_match.group(1).rstrip('/')
    for cf in changed_files:
        cf = cf.strip()
        if not cf:
            continue
        # Normalize: both may or may not start with 'vllm/'
        if key_path in cf or cf in key_path:
            return True
    return False


def _format_path_section(arch: dict, changed_files: list[str]) -> list[str]:
    """Format path-level mapping (patch_impact_map + definitely_affected_paths)."""
    lines: list[str] = []
    cpr = arch.get("cross_project_relationship") or {}
    pim = cpr.get("patch_impact_map") or {}
    matched_pim = {k: v for k, v in pim.items() if _match_path(k, changed_files)}
    if matched_pim:
        lines.append("- patch_impact_map (vllm path -> ascend patch file):")
        for k, v in matched_pim.items():
            lines.append(f"  - `{k}` -> {v}")

    ijr = cpr.get("impact_judgment_rules") or {}
    dap = ijr.get("definitely_affected_paths") or []
    matched_dap = [p for p in dap if _match_path(p, changed_files)]
    if matched_dap:
        lines.append("- definitely_affected_paths (base classes vllm-ascend subclasses):")
        for p in matched_dap:
            lines.append(f"  - {p}")
    return lines


def _format_interface_section(arch: dict, changed_files: list[str]) -> list[str]:
    """Format interface-level mapping (inheritable_interfaces)."""
    lines: list[str] = []
    ifs = arch.get("interface_surface") or {}
    inheritable = ifs.get("inheritable_interfaces") or []
    matched = []
    for iface in inheritable:
        # iface may have 'base_class_path' or 'file' field
        base_path = iface.get("base_class_path") or iface.get("file") or ""
        if base_path and _match_path(base_path, changed_files):
            matched.append(iface)
    if not matched:
        return lines
    lines.append("- inheritable_interfaces (vllm base class -> ascend subclass):")
    for iface in matched:
        name = iface.get("name") or iface.get("base_class") or "?"
        ascend_impl = iface.get("ascend_impl") or ""
        key_methods = iface.get("key_methods") or []
        lines.append(f"  - {name} -> {ascend_impl}")
        if key_methods:
            lines.append(f"    key_methods: {', '.join(key_methods)}")
    return lines


# Subsystem classification: vllm changed-file keyword -> subsystem category.
# Used to decide which MCP tools to call per step (approach 3: conditional).
# Categories cover the subsystems SKILL.md flags as "NEVER a no-op"
# (multimodal/transformers_utils/processors) plus the core patchable
# subsystems (platform/attention/worker/moe/distributed/config/compilation).
_CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("platform", ["vllm/platforms/"]),
    ("attention", ["vllm/v1/attention/", "vllm/model_executor/layers/attention/",
                   "mla", "mamba_attn", "attention/backends"]),
    ("worker", ["vllm/v1/worker/", "model_runner", "worker_base",
                "vllm/v1/core/sched", "vllm/v1/engine",
                "vllm/v1/spec_decode/"]),
    ("moe", ["fused_moe", "routed_experts", "moe/"]),
    ("distributed", ["vllm/distributed/", "kv_cache", "kv_transfer",
                     "device_communicators"]),
    ("config", ["vllm/config", "vllm/engine/", "vllm/envs"]),
    ("compilation", ["vllm/compilation/"]),
    ("multimodal", ["vllm/multimodal/", "vllm/transformers_utils/",
                    "processors/", "vllm/inputs/", "vllm/lora/"]),
    ("models", ["vllm/model_executor/models/"]),
]


def _classify_changed_files(changed_files: list[str]) -> list[str]:
    """Classify a step's changed vllm files into subsystem categories.

    Returns list of category names (platform/attention/worker/moe/
    distributed/config) whose keywords match any changed file.  Used to
    decide which MCP tools to call - only call tools for subsystems the
    step actually touches, keeping the injected context bounded.
    """
    matched: list[str] = []
    for cat, keywords in _CATEGORY_RULES:
        if any(kw in f for f in changed_files for kw in keywords):
            matched.append(cat)
    return matched


def load_vllm_report_context(
    report_dir: Path,
    vllm_target_commit: str,
    changed_files: list[str],
    ascend_path: str | Path | None = None,
) -> str:
    """Return markdown snippet of vllm-report impact for this step.

    DETERMINISTIC MCP tool calls: imports vllm-report's mcp_server_app and
    calls its tool functions directly (no model involvement, no MCP protocol).
    Per step, calls:
      1. tool_get_adaptation_guide(sha)      - commit-level guide (markdown)
      2. tool_get_cross_project_mapping()    - full vllm<->ascend mapping
      3. tool_get_interface_surface("vllm-ascend") - inheritable interfaces
      4. tool_get_patch_catalog()            - patch catalog (targets/why/how)

    The tool functions are vllm-report's curated implementations (markdown
    formatting, cross-date search) - better than re-implementing JSON reads.
    Falls back to JSON direct-read if import fails.

    Returns empty string on any error - flow degrades to grep.
    """
    if not report_dir or not Path(report_dir).is_dir():
        return ""
    report_dir = Path(report_dir)
    sections: list[str] = []
    guide_missing = False

    # Try deterministic MCP tool function calls first (curated output).
    try:
        import sys as _sys
        import asyncio as _asyncio
        _sys.path.insert(0, str(report_dir / "src"))
        import mcp_server_app

        mcp_server_app.data_dir = str(report_dir / "data")
        if ascend_path:
            mcp_server_app.ascend_repo_path = str(Path(ascend_path).resolve())

        # CONDITIONAL tool selection (approach 3): call tools based on which
        # vllm subsystems this step's changed_files touch.  get_adaptation_guide
        # is always called (small, high value); the rest are called only when
        # relevant, keeping prompt size bounded (vs. unconditional all-4 which
        # injected ~28K chars/step, most of it noise for the step).
        categories = _classify_changed_files(changed_files)
        ts_print(f"\n[vllm_report] changed_files classified as: {categories or '(none)'}")

        if vllm_target_commit:
            ts_print(f"[vllm_report] → calling MCP tool: "
                     f"tool_get_adaptation_guide({vllm_target_commit[:8]})")
            guide = _asyncio.run(
                mcp_server_app.tool_get_adaptation_guide(vllm_target_commit))
            guide_lines = len(guide.splitlines()) if guide else 0
            ts_print(f"[vllm_report] ← tool_get_adaptation_guide returned "
                     f"{guide_lines} lines")
            if guide and "未找到" not in guide:
                sections.append("### Commit adaptation guide")
                sections.append(guide)
            else:
                guide_missing = True

        # Cross-project mapping: platform/distributed/config/compilation/
        # multimodal/models subsystems all wire through vllm-ascend's
        # platform patches - the mapping tells the adapter where.
        if any(c in categories for c in ("platform", "distributed", "config",
                                         "compilation", "multimodal", "models")):
            ts_print("[vllm_report] → calling MCP tool: "
                     "tool_get_cross_project_mapping()")
            mapping = _asyncio.run(mcp_server_app.tool_get_cross_project_mapping())
            map_lines = len(mapping.splitlines()) if mapping else 0
            ts_print(f"[vllm_report] ← tool_get_cross_project_mapping returned "
                     f"{map_lines} lines")
            if mapping:
                sections.append("### Cross-project mapping")
                sections.append(mapping)

        if "attention" in categories:
            ts_print('[vllm_report] → calling MCP tool: '
                     'tool_get_interface_surface("vllm-ascend")')
            iface = _asyncio.run(
                mcp_server_app.tool_get_interface_surface("vllm-ascend"))
            iface_lines = len(iface.splitlines()) if iface else 0
            ts_print(f"[vllm_report] ← tool_get_interface_surface returned "
                     f"{iface_lines} lines")
            if iface:
                sections.append("### Interface surface")
                sections.append(iface)

        if "worker" in categories or "moe" in categories:
            ts_print('[vllm_report] → calling MCP tool: '
                     'tool_get_patch_catalog("worker")')
            catalog = _asyncio.run(mcp_server_app.tool_get_patch_catalog("worker"))
            cat_lines = len(catalog.splitlines()) if catalog else 0
            ts_print(f"[vllm_report] ← tool_get_patch_catalog(worker) returned "
                     f"{cat_lines} lines")
            if catalog:
                sections.append("### Patch catalog (worker)")
                sections.append(catalog)

        if "platform" in categories:
            ts_print('[vllm_report] → calling MCP tool: '
                     'tool_get_patch_catalog("platform")')
            catalog = _asyncio.run(mcp_server_app.tool_get_patch_catalog("platform"))
            cat_lines = len(catalog.splitlines()) if catalog else 0
            ts_print(f"[vllm_report] ← tool_get_patch_catalog(platform) returned "
                     f"{cat_lines} lines")
            if catalog:
                sections.append("### Patch catalog (platform)")
                sections.append(catalog)

        # Commit stub appended AFTER all tool calls - only when MCP produced
        # nothing else.  This keeps the JSON direct-read fallback (below) from
        # being suppressed by a stub that doesn't provide real info.
        if vllm_target_commit and guide_missing and not sections:
            sections.append(f"### Commit analysis ({vllm_target_commit[:8]})")
            sections.append("- No per-commit analysis in vllm-report yet "
                            "(may be too recent). Use grep to verify impact.")

    except Exception as e:
        ts_print(f"\n[vllm_report] MCP tool call failed ({e}), falling back to JSON direct-read")

    # JSON direct-read fallback: only runs when MCP produced NOTHING (import
    # failure or all tool calls raised).  If MCP partially succeeded, the
    # missing sections would duplicate info - skip fallback then.
    if not sections:
        # 1. commit-level
        commit = _find_commit_analysis(report_dir, vllm_target_commit) if vllm_target_commit else None
        if commit:
            sha = commit.get("sha", "")[:8]
            sections.append(f"### Commit analysis ({sha})")
            sections.append(f"- message: {(commit.get('message') or '').strip()[:150]}")
            sections.append(f"- tags: {', '.join(commit.get('tags') or [])}")
            sections.extend(_format_commit_section(commit))

        # 2 + 3. path-level + interface-level (both from architecture.json)
        arch_path = report_dir / "data" / "vllm-ascend" / "context" / "architecture.json"
        if arch_path.is_file():
            try:
                arch = json.loads(arch_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                arch = {}
            if arch:
                path_lines = _format_path_section(arch, changed_files)
                if path_lines:
                    sections.append("### Path-level mapping")
                    sections.extend(path_lines)
                iface_lines = _format_interface_section(arch, changed_files)
                if iface_lines:
                    sections.append("### Interface-level mapping")
                    sections.extend(iface_lines)

    if not sections:
        return ""

    out = "\n".join(sections)
    # Cap at 150 lines
    out_lines = out.splitlines()
    if len(out_lines) > 150:
        out = "\n".join(out_lines[:150]) + "\n... (truncated, see vllm-report for more)"
    return out
