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

**Format rules — apply WHILE editing, not after:**

- Every line **must** be ≤ 120 characters.  When replacing `== "X"` with
  `in ("X", "Y")`, the line WILL exceed 120 — break it BEFORE committing
  the edit.  Use intermediate variables or split across lines.
- No unused imports (F401).  If you `import X`, use X.  If you remove the
  last use, remove the import.
- No unused local variables (F841).  Every assigned variable is either
  used or prefixed with `_`.
- No undefined names (F821).  When using `vllm_version_is()`, make sure
  `from vllm_ascend.utils import vllm_version_is` is in the file — this
  is the #1 cause of F821 in main2main adaptations.  Every new symbol
  used must be imported or defined in the same file.
- Imports sorted per `ruff` rules (stdlib → third-party → first-party).
- If unsure, run `bash format.sh` to auto-fix what it can, then fix any
  remaining violations before marking the adaptation complete.
- See `reference/common-pitfalls.md` for the output-buffer trap, indentation
  errors, and other frequent adaptation mistakes.

When the upstream patch removes a parameter from a method signature
and the old code used it as a mutable output buffer (e.g. `output[:] =
result`), this is a **dual change**: both the parameter AND the return
semantics changed.  Do NOT just redirect the method — you MUST:

1. Make the removed parameter optional (`= None`)
2. Guard the return path: old branch writes to `output[:]`, new branch returns the result
3. Guard EVERY call site that passes `output=...` — the upstream now calls WITHOUT it

A single-line redirect like `_PATCH_TARGET.forward = AscendVersion.forward`
is NEVER the right answer for this pattern.  See `reference/adaptation-patterns.md` §1b
for the correct before/after code.

### fix mode

The working tree already contains the failed adaptation — do NOT start from
scratch.

**Pre-CI failures**: the inlined error content is `pre_ci_check.json` — each
failed check has `violations` with exact file:line:col:CODE.

  - **format violations (E501/F821/etc.)**: Open the file, go to the reported
    line number, and manually edit the code.  ruff-format CANNOT auto-fix these.
    After fixing, re-run `bash format.sh` to verify.  Repeat until format.sh
    exits clean.  This is a hard blocker — do not skip.

  - **version_strings / broken_imports**: violations tell you exactly what's
    wrong (wrong release tag, missing import).  Fix those specific lines.

**E2E test failures**: the inlined error content is `round-N-result.json`.
Open it, check `code_bugs_count` > 0 → open failed tests from
`suite_results[test_name]`.  For each failed test, read BOTH files
referenced by `log_path` and `summary_path`:
  - `-summary.json` → structured `code_bugs`/`env_flakes` arrays (traceback + classification)
  - `.log` → raw pytest output (full traceback when the summary is insufficient)

See `reference/common-pitfalls.md` §"Fix mode cheat sheet" for the ruff
error code table and error-to-upstream trace workflow.

**Format/lint violations (E501, F821, etc.)** — NON-NEGOTIABLE.  ruff-format
CANNOT auto-fix these.  Re-running format.sh will NOT resolve them.  You
MUST open the file, go to the exact line number reported in the error, and
manually edit the code.  After fixing, run format.sh again to verify.
Do NOT proceed to any other task until every format error is resolved.

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
