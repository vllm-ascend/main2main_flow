---
name: adapter
description: Adapt vllm-ascend to upstream vLLM changes ({mode} mode).
---
# adapter — mode: {mode}

## Repositories

| repo | path |
|------|------|
| vllm (read-only) | {vllm_path} |
| vllm-ascend (edit here) | {ascend_path} |

## Inputs

| field | value |
|-------|-------|
| mode | {mode} |
| step | {step_id} |
| last step | {is_last_step} |
| release tag | {release_tag} |
| upstream patch | {patch_path} |
| changed files | {changed_files_path} |
| archive dir | {step_dir} |

## Error Content (inlined)

{error_content}

## Rules

- Only modify vllm-ascend at {ascend_path} (never vLLM at {vllm_path})
- Do NOT run git add, git commit, git reset, or git checkout in vllm-ascend
- Use `vllm_version_is("{release_tag}")` for version boundaries — never `hasattr` or `try/except`
- All branches of a version guard must have identical function signatures
- Static analysis only — do not import vllm/vllm-ascend, run tests, launch models, or require NPU/GPU
- Never read raw CI logs — use inlined error content above
- Do NOT treat ModuleNotFoundError or missing NPU/GPU from local commands as adaptation failures

## Cumulative Step Model

The vllm-ascend working tree already contains all successful adaptations from previous steps:
1. Read {previous_step_summary_path} if it exists
2. Reuse prior guards, helpers, imports, and patterns
3. Never revert prior adaptations unless the current change proves them obsolete

The step_target.patch is cumulative (git diff HEAD).

## Code Exploration

- Start from the upstream patch and changed-file list — these are the signal.
  **Key question**: does vllm-ascend subclass, override, call, import, or read
  anything this patch changed? Internal upstream changes only need adaptation
  when vllm-ascend directly depends on the behavior.
- Use the File Mapping Table in code-structure-guide.md to route changed
  upstream paths to likely vllm-ascend files. If codegraph tools are available,
  prefer them; if not, use grep/glob/file reads. Do not stall on missing tools.
- Read enough of the vllm-ascend code to understand how the subsystem works —
  subclass chains, registration patterns, import structure. Skimming the
  relevant module is better than making a wrong assumption.
- When an upstream change touches an interface that vllm-ascend implements,
  read the upstream base class or caller to understand the contract change.
- **Processor/multimodal trap**: if `changed_files_path` contains
  `vllm/transformers_utils/processors/__init__.py` or `vllm/multimodal/`,
  this is NEVER a no-op. Search vllm-ascend for `*processor*compat*.py` and
  verify every compat patch still works.
  See `reference/adaptation-patterns.md` §12.
- Use `grep` and `glob` to verify that no other vllm-ascend file depends on
  the same changed symbol. When a method signature changes, grep for ALL
  `def <method_name>(` in the vllm-ascend tree — every override must be updated.

## Workflow

### adapt mode

1. Read the upstream patch and changed file list from `{patch_path}` and `{changed_files_path}`
2. Use targeted search to find the impacted vllm-ascend code (see Code Exploration)
3. Apply minimal changes — do not refactor unrelated code

**Guard decision tree**:

```
Does this code path need to support BOTH the release version AND upstream main?
  ├─ YES, and the API differs → use vllm_version_is("{release_tag}")
  └─ NO  → no new guard needed
```

- New parameters with defaults: no guard needed
- Constructor/factory signature changes: guard with `vllm_version_is()`
- Import path changes: guard the import, import unconditionally if the symbol
  exists in both versions

**BEFORE marking the adaptation complete, verify ALL of these:**

1. `bash format.sh` runs clean (no FAILED hooks, no ruff errors)
2. Every `vllm_version_is` guard: NEW upstream-main code is in `else`/`not`
   branch, OLD release code is in `if` branch.
3. Every guarded `from vllm.X import Y` line has `# type: ignore[import-not-found]`
4. Imports that don't exist on the OLD vllm version: the import of the new
   class MUST be inside `else` (not guarded with `# type: ignore`).
5. No circular imports
6. Every call site passes correct number, type, AND ORDER of arguments on
   BOTH version branches. Use keyword arguments for new parameters.
7. Override methods match the upstream signature.
8. No variable aliases as base classes — use `TypeAlias` or direct class name.
9. When fixing a version-branch bug, grep for the same pattern in ALL sibling
   functions and fix them all in the same commit.
10. When a method signature changed, grep for ALL `def <method_name>(` in the
    codebase — every override must be updated.
11. Every `next(gen, default)` has a default value — no bare `next(...)`.
12. `super().__init__()` called in every subclass `__init__`.
13. No exact version matching (`== "X.Y.Z"`).
14. No dead code, commented-out blocks, or stale `# type: ignore` left behind.
15. See `reference/common-pitfalls.md` §"Additional QA-level checks" for
    remaining items (registries, Triton params, getattr, path resolution, etc.).

**Format rules — apply WHILE editing, not after:**

- Every line **must** be ≤ 120 characters.
- No unused imports (F401), unused variables (F841), undefined names (F821).
- Every `vllm_version_is()` call needs `from vllm_ascend.utils import vllm_version_is`.
- Imports sorted per `ruff` rules (stdlib → third-party → first-party).

**Output-buffer trap**: When upstream changes from `output[:] = result` to
`return result`, don't just redirect a forward method. You MUST make the
removed parameter optional, guard the return path, and guard every call site.
See `reference/adaptation-patterns.md` §1b.

### fix mode

The working tree already contains the failed adaptation — do NOT start from
scratch. Make minimal targeted fixes to the specific errors reported.

**Pre-CI failures**: open `pre_ci_check.json` → each failed check has
`violations` with exact file:line:col:CODE. Fix those specific lines.

**E2E test failures**: open `round-N-result.json` → check `code_bugs_count` > 0
→ open failed tests from `suite_results[test_name]`. Read both `-summary.json`
(structured code_bugs/env_flakes) and `.log` (raw traceback).

## Output

Write to {step_dir}/:

| file | content |
|------|---------|
| analysis.md | subsystems touched, changes, version guard assessment |
| step_summary.md | cumulative summary (preserve prior, append `{step_id}` section) |
| result.json | `{{"status": "adapted" \| "noop", "files_touched": [...]}}` — write LAST |

### step_summary.md

No-op — ONE line: `- {step_id}: No-op — <reason>`

Adapted:
```
- {step_id}: Adapted — <files>
  Upstream source: [<sha>](https://github.com/vllm-project/vllm/commit/<sha>)
  Cause: <what changed upstream — 1-2 sentences on the upstream diff>
  Change: <what was done in vllm-ascend — specific files, guards, new params>
```

**Cause vs Change — they must be DIFFERENT:**
- **Cause** = what the upstream vLLM commit changed
- **Change** = what vllm-ascend did to adapt

Multi-line fields: indent continuation lines with 2 spaces.

Do NOT write the same text for both fields.

## Last Step Only

If {is_last_step}: check code-structure-guide.md freshness. If stale, write updated version as {step_dir}/{code_structure_guide_file}.

## Reference

{reference_content}