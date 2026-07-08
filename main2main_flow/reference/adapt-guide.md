# Adapt Guide

Use this guide during the adapt phase of each main2main step. The goal is not
to copy upstream vLLM changes into vllm-ascend. The goal is to understand which
upstream contracts changed, then update the Ascend implementation that depends
on those contracts.

This file is only about adaptation decisions and code changes. Mechanical
pipeline work, such as updating the pinned vLLM commit reference, generating
patches, running pre_ci_check, running e2e tests, and committing changes, is
handled externally by the main2main flow.

---

## Re-orient (every step, not just the first)

Re-read this file at the start of every step. For code-structure routing, use
`reference/code-structure-guide.md` only when you need to map changed upstream
paths/symbols to likely vllm-ascend files.

Before starting, confirm:
- Current step and upstream range from the prompt
- Compatible release tag from the prompt — needed for any `vllm_version_is()` guards
- The prompt-provided paths for `changed_files_path`, `patch_path`, and `step_dir`
- The previous step summary path, if present, so this step can reuse prior work

---

## Cumulative step model

Each step runs on the same vllm-ascend working tree. Successful changes from
previous steps are already present. Do not reinitialize, revert, or duplicate
those changes. Read the previous step summary path from the prompt when it
exists, then reuse prior version guards, helper functions, imports, and
adaptation patterns.

The per-step `step_target.patch` is generated externally from `git diff HEAD` and
is cumulative from the original vllm-ascend base HEAD through the current step.

---

## Inputs

For each step, use the prompt-provided paths:

- `changed_files_path` / `changed_files.txt` — file paths changed by the upstream step
- `patch_path` / `upstream.patch` — full upstream diff for the step
- `step_dir` — archive directory for `analysis.md`, `review.md`, and `step_summary.md`

Read `changed_files.txt` first. It is a cheap routing signal that tells you
which parts of `upstream.patch` deserve attention.

---

## Step 1: Analyze vLLM Changes

1. Read `changed_files.txt`.
2. When the changed upstream paths/symbols are not obvious, consult
   `reference/code-structure-guide.md` to identify key areas and likely
   vllm-ascend locations. Do not read the whole structure guide unless needed.
3. Find the relevant chunks in `upstream.patch` and identify the concrete change:
   new/removed abstract methods, changed signatures, renamed config fields, moved
   imports, changed constructor args, dependency bumps, or changed return types.
4. Use the structure guide's File Mapping Table to find likely vllm-ascend
   locations that need adaptation.

The key question: **does vllm-ascend subclass, override, call, import, or read
anything this patch changed?** Internal implementation changes only need
adaptation when vllm-ascend directly depends on the behavior.

---

## Upstream change type → adaptation

| Upstream change | Action in vllm-ascend | Guard needed? |
|:---|:---|:---|
| New abstract `Platform` method | Implement in `vllm_ascend/platform.py` now — missing methods fail at runtime, not import | No — additive method is inert on the release version |
| Config field moved/renamed | Update every vllm-ascend read site (grep the old name) | Yes, if the same code path runs on both versions |
| Constructor / dataclass change | Fix call sites to the new shape | Only if both shapes must run |
| Module moved/renamed | Update imports | Yes — branch the import with `vllm_version_is` |
| Upstream deletes a module that `vllm_ascend/patch/` patches | Remove or guard the patch (see "Adapting vllm_ascend/patch/") | Guard if the patch is still needed on the release version |
| Dependency bump | Mirror only if vllm-ascend code requires it | No |

---

## Step 2: Adapt vLLM Ascend Project

For each related change in vLLM, evaluate whether adaptation in vLLM Ascend is
needed:

- **Internal Architecture Changes**
  Check internal interfaces of vLLM core modules (scheduler, executor, model runner, etc.)
  Update vLLM Ascend's Ascend-specific implementations (e.g., NPU worker/model runner,
  custom attention, custom ops)
  Preserve vLLM Ascend specific modifications (e.g., code under vllm_ascend/)

- **Dependency Changes**
  Check for dependency version changes in pyproject.toml or setup.py, but do not
  blindly mirror upstream vLLM dependency bumps. Only update vLLM Ascend
  dependency declarations when the change is required by vllm-ascend code or by
  the external validation flow.

- **Version Compatibility**

  Every signature change, config field move, or import path change is a potential
  version boundary. Use a guard only when vllm-ascend must support both the
  release API and the new upstream API in the same codebase, and the affected
  code path can run against both versions.

  ```
  Does this vllm-ascend code path need to support both release and upstream main?
    ├─ YES, and the API differs → wrap behavior with vllm_version_is("<release_tag>")
    └─ NO, or an enclosing guard already separates behavior → no new guard needed
  ```

  No guard is needed when the upstream change is internal and vllm-ascend does
  not call, override, import, or read it. When unsure, search existing patterns in
  source and follow the same import style, version string, and branching
  structure. All branches of a version guard must keep identical public function
  signatures.

When a feature genuinely can't be supported on Ascend yet, add a stub with a
`# TODO` comment referencing the issue.

A no-op adapt (nothing to change) is fine, but still write `analysis.md`,
`review.md`, and `step_summary.md` explaining why no vllm-ascend code change was
needed. The main2main flow will still run pre_ci_check and `_run_e2e_test`
externally.

---

## Adapting `vllm_ascend/patch/`

`vllm_ascend/patch/` monkey-patches upstream symbols and is the most fragile
coupling to vLLM. How it works (see the header of `vllm_ascend/patch/__init__.py`):

- Two subpackages: `patch/platform/` (applied before workers start, via
  `adapt_patch(is_global_patch=True)` in `NPUPlatform.pre_register_and_update()`)
  and `patch/worker/` (applied in each worker's `__init__` via
  `adapt_patch(is_global_patch=False)`); `adapt_patch` is in `vllm_ascend/utils.py`.
- Registration is an import side effect: each `patch_*.py` module imports the
  upstream `vllm.*` module and reassigns attributes on it at import time. The
  patch is active only if imported in `patch/platform/__init__.py` or
  `patch/worker/__init__.py`, and every patch must be described in
  `vllm_ascend/patch/__init__.py`.
- When an upstream step touches a module, grep `vllm_ascend/patch/` for imports
  of that module to check whether a patch target still exists at the new commit
  (read the vllm tree; a patch of a deleted/renamed symbol fails at import or
  silently patches nothing).
- Delete vs guard: if the patch is obsolete on both versions, remove the patch
  file, its import, and its description entry. If it is still needed on the
  release version only, guard its import in the subpackage `__init__.py` —
  existing pattern: `if vllm_version_is("0.23.0"): import ...` in
  `vllm_ascend/patch/platform/__init__.py`.

---

## Guard lifecycle

Guards are added against the current release tag from the prompt. They are
removed only when the tag advances to a release that contains the new behavior —
never during a main2main run. Before deleting any guard branch or the helper it
calls, grep the full call chain: multiple patch files may depend on it
(review-lessons §1.4 / §1.5).

---

## Step 3: Static Self Review

Operational constraints (no tests, no imports, no git commands) are in the task
prompt. During this AI step, only do static review:
- Inspect the vllm-ascend diff and relevant source files
- Verify version guards use the release tag from the prompt
- Verify guarded branches keep identical public function signatures
- Verify imports by reading source, not by importing vllm/vllm-ascend locally
- Record findings in `analysis.md`, `review.md`, and `step_summary.md`

For adapt mode, `analysis.md` should include:
- Upstream files changed and relevant upstream contracts identified
- vllm-ascend files checked through the File Mapping Table
- `Checked but unchanged` notes for relevant vllm-ascend files that did not need
  edits, with the reason they were unaffected
- Adaptation plan and implemented changes, or no-op rationale
- Version guard decisions and release tag used

For adapt mode, `review.md` should include:
- Static diff review result
- Guard, signature, import, and config-access checks
- Remaining risks or explicit "no known issues"

---

## Code structure routing reference

The vLLM key areas, vllm-ascend key file locations, and File Mapping Table live in
`reference/code-structure-guide.md`.

Use that file as an on-demand routing reference when changed upstream paths or
symbols need mapping to vllm-ascend code. It is intentionally separate from this
workflow guide because it describes relatively stable code structure and can be
refreshed independently.

---

## Common pitfalls

### Importing modules that don't exist (yet)

**Symptom**: Code adds `from vllm.X import Y` but `X` has been renamed, moved, or
doesn't exist in the target version. mypy reports `import-not-found`, breaking CI
lint checks.

**Prevention**: Before writing any `import` from vllm, verify the module exists
in the upstream tree:
```bash
grep -r "class Y\|def y_func" ${VLLM_DIR}/vllm/ | head -5
find ${VLLM_DIR}/vllm -name "X.py"
```

If the module doesn't exist at the expected path, search for where the symbol
was moved to, or use a version guard (`vllm_version_is`) to import conditionally.

### Patching symbols from deleted upstream modules

**Symptom**: A patch file patches `vllm.X.Y` but `vllm.X` was deleted upstream.
The patch silently fails or raises `ModuleNotFoundError` at runtime.

**Prevention**: Check whether the upstream file still exists before patching:
```bash
test -f "${VLLM_DIR}/vllm/X.py" || echo "MODULE DELETED"
```
If deleted, remove the corresponding vllm-ascend patch and note the reason in
`step_summary.md`.
