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

## Code Exploration

If codegraph tools are available, prefer them; if not, use grep/glob/file reads. Do not stall on missing tools.

## Cumulative Step Model

The vllm-ascend working tree already contains all successful adaptations from previous steps:
1. Read {previous_step_summary_path} if it exists
2. Reuse prior guards, helpers, imports, and patterns
3. Never revert prior adaptations unless the current change proves them obsolete

The step_target.patch is cumulative (git diff HEAD).

## Code Exploration

- Start from the upstream patch and changed-file list — these are the signal
- Use the File Mapping Table in code-structure-guide.md to route changed
  upstream paths to likely vllm-ascend files
- Read enough of the vllm-ascend code to understand how the subsystem works —
  subclass chains, registration patterns, import structure.  Skimming the
  relevant module is better than making a wrong assumption
- When an upstream change touches an interface that vllm-ascend implements,
  read the upstream base class or caller to understand the contract change
- Use `grep` and `glob` to verify that no other vllm-ascend file depends on
  the same changed symbol

## Workflow

### adapt mode

1. Read the upstream patch and changed file list from `{patch_path}` and `{changed_files_path}`
2. Use targeted search to find the impacted vllm-ascend code (see Code Exploration)
3. Apply minimal changes — do not refactor unrelated code

### fix mode

The working tree already contains the failed adaptation — do NOT start from
scratch.  See `reference/diagnosis-guide.md` for the full fix workflow.

**Format/lint violations (E501, F821, etc.)**: ruff-format CANNOT auto-fix
these.  You MUST open the file at the reported line number and manually
edit the code.  Re-running format.sh will NOT resolve them.

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
  Upstream commit: <sha>
  Cause: <what changed upstream>
  Change: <what was done in vllm-ascend>
```

Do NOT list "files checked but unchanged".

## Last Step Only

If {is_last_step}: check code-structure-guide.md freshness. If stale, write updated version as {step_dir}/{code_structure_guide_file}.

## Reference

{reference_content}

## RECAP

- Deliverables: analysis.md, step_summary.md, result.json → {step_dir}/
- Never modify vllm ({vllm_path})
- Only `vllm_version_is("{release_tag}")` guards; identical signatures across branches
- No git add/commit/reset/checkout
- Static analysis only — never run tests or import the packages
