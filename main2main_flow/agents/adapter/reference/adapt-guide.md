# Adapt Guide

Use this guide to understand how to work through each main2main step. For
concrete code patterns, see `reference/adaptation-patterns.md`. For mistakes
to avoid, see `reference/common-pitfalls.md`. For file routing, see
`reference/code-structure-guide.md`.

Mechanical pipeline work (commit reference update, patches, pre_ci, e2e
tests, commits) is handled externally by the main2main flow.

---

## Cumulative step model

Each step runs on the same vllm-ascend working tree. Successful changes from
previous steps are already present. Do not reinitialize, revert, or duplicate
those changes. Read the previous step summary when it exists — reuse prior
version guards, helpers, imports, and adaptation patterns.

The per-step patch is cumulative (`git diff HEAD`). Do not run git add, git
commit, git reset, or git checkout in vllm-ascend.

---

## Inputs

For each step, the prompt provides:

| field | content |
|---|---|
| `changed_files_path` | upstream files changed in this step (read first) |
| `patch_path` | full upstream diff for this step |
| `step_dir` | archive directory for `analysis.md` and `step_summary.md` |

---

## Workflow

### 1. Analyze

1. Read `changed_files.txt` first — it tells you which parts of the
   upstream patch matter
2. Find relevant chunks in the upstream patch: new/removed methods, signature
   changes, renamed config fields, moved imports, constructor arg changes
3. Use the File Mapping Table in `reference/code-structure-guide.md` to
   route changed upstream paths to likely vllm-ascend locations

**Key question**: does vllm-ascend subclass, override, call, import, or
read anything this patch changed? Internal upstream changes only need
adaptation when vllm-ascend directly depends on the behavior.

### 2. Adapt

For each affected location, determine the right approach. See
`reference/adaptation-patterns.md` for concrete patterns — the decision
rules below tell you WHEN each applies, the patterns file tells you HOW.

### 3. Guard decision

```
Does this code path need to support BOTH the release version AND upstream main?
  ├─ YES, and the API differs → use vllm_version_is("<release_tag>")
  └─ NO  → no new guard needed
```

- New parameters with defaults: no guard needed
- Constructor/factory signature changes: guard with `vllm_version_is()`
- Import path changes: guard the import, import unconditionally if the
  symbol exists in both versions
- `hasattr` and `try/except` are NOT acceptable version guards

All branches of a guard must have identical public function signatures.
When unsure, search existing `vllm_version_is` usage in the codebase and
follow the same style.

### 4. No-op steps

If the upstream changes don't affect vllm-ascend, write `analysis.md`
explaining why and a one-line `step_summary.md` entry. No `review.md` — the
adapter-qa handles review independently.

**Processor/multimodal exception**: when `changed_files.txt` includes
`vllm/transformers_utils/processors/__init__.py` or `vllm/multimodal/`,
this is NOT a no-op. The upstream change may remove processor registry
entries or change tokenizer attribute requirements. Search vllm-ascend
for `*processor*compat*.py` and verify the compat patch still works with
the new upstream code. See `reference/adaptation-patterns.md` §12 and
`reference/common-pitfalls.md` §"Processor/multimodal compat patch blocked
by early return".

---

## Outputs

Write to `{step_dir}/`:

| file | content |
|------|---------|
| `analysis.md` | upstream contracts changed, adaptation rationale, guard decisions |
| `step_summary.md` | one line for no-op; Cause / Change for adapted (no "checked but unchanged") |
| `result.json` | `{"status": "adapted" \| "noop", "files_touched": [...]}` |
