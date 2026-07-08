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
Do not run git add, git commit, git reset, or git checkout in vllm-ascend.

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

## Step 3: Static Self Review

Do not run pre_ci_check.py, tests, imports, model launches, or runtime validation
manually. The main2main flow runs pre_ci_check automatically after each opencode
attempt, and `_run_e2e_test` handles real validation later.

During this AI step, only do static review:
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

---

## Adaptation Patterns

Patterns extracted from successful main2main PRs. For each upstream change
type a correct adaptation approach and anti-pattern.

### 1. Upstream adds a parameter to a function vllm-ascend calls or overrides

**Rule**: Add the parameter as a keyword argument with a default value.
No version guard needed.

```python
# Upstream adds `reduce_results: bool = True`
def _deepseek_v2_mla_attention_init(
    ...,
    reduce_results: bool = True,   # ← add with same default
) -> None:
```

> Anti-pattern: wrapping the entire function body in a version guard just for
> a new parameter.

### 2. Upstream changes a constructor / factory signature

**Rule**: Guard with `vllm_version_is()`, pass the new parameter shape in the
new-version branch, keep the old shape in the else branch.

```python
# Upstream: WeightTransferEngine.__init__ now takes VllmConfig
if vllm_version_is("0.23.0"):
    super().__init__(config, parallel_config, vllm_config=vllm_config)
else:
    super().__init__(config, parallel_config, model=model)
```

> Each branch independently calls super() with its version's signature.
> Don't try to unify the two shapes — they may diverge more later.

### 3. Upstream moves a class to a different module

**Rule**: Use version-guarded conditional imports.  For classes that exist in
both versions, import unconditionally (no guard).

```python
# UnquantizedFusedMoEMethod moved between modules:
if vllm_version_is("0.23.0"):
    from vllm...fused_moe.layer import UnquantizedFusedMoEMethod
else:
    from vllm...fused_moe.unquantized_fused_moe_method import UnquantizedFusedMoEMethod

# FusedMoE and MoERunner exist in both — import unconditionally:
from vllm...fused_moe.layer import FusedMoE, MoERunner
```

> Verify the import path exists before writing the guard: check the upstream
> tree at the target commit for both the old and new paths.

### 4. Upstream deletes a long-standing module that vllm-ascend patches

**Rule**: Remove the vllm-ascend patch file entirely.  A patch against a
deleted upstream module is dead code — deleting it is the correct adaptation.

> Anti-pattern: wrapping the entire patch file body in `if False:` or a
> never-satisfied guard.  Just delete it. The step summary explains why.

### 5. Upstream fixes a bug that vllm-ascend has a workaround for

**Rule**: Aggressively remove the workaround.  Simplify any conditional logic
that was only needed because of the fixed bug.

```python
# Before (workaround for upstream parameter-aliasing bug):
def _needs_routed_expert_parameter_aliases(self) -> bool:
    # 20 lines matching model_type, architectures, layer_name patterns
    return model_type in {...10+ model types...}

# After (upstream fixed the bug, only gpt_oss still needs the shim):
def _needs_routed_expert_parameter_aliases(self) -> bool:
    return getattr(hf_config, "model_type", None) == "gpt_oss"
```

> Each upgrade, audit existing `vllm_version_is` guards and ask: which
> branches can now be collapsed?

### 6. Upstream refactoring is too large for inline guards (>~50 lines diff)

**Rule**: Split into two files.  One for the release version, one for main.
Use `vllm_version_is()` at import time to select.

```
vllm_ascend/ops/fused_moe/fused_moe.py         ← for main (615+/622-)
vllm_ascend/ops/fused_moe/fused_moe_0_23_0.py  ← copy of pre-refactor file
```

Then register the patch in `patch/platform/__init__.py`:
```python
if not vllm_version_is("0.23.0"):
    from vllm_ascend.patch.platform.patch_fused_moe import ...
```

> When in doubt, prefer inline guards.  Only split when the file becomes
> unreadable or the two versions fundamentally diverge.

### 7. Upstream adds a runtime check that Ascend cannot satisfy

**Rule**: Register a no-op stub that passes the check.  Annotate with "this is
not a functional implementation — it exists only to satisfy the upstream guard."

```python
# Upstream MoE init checks `all2all_manager is not None`
# Ascend doesn't use all2all — register a dummy to bypass the check:
self.all2all_manager = None  # no-op: bypass upstream MoE fault-tolerance check
```

> The stub must have the expected type so attribute access doesn't raise.

### 8. Upstream uses a PyTorch API that doesn't work on NPU

**Rule**: Redirect to the equivalent NPU-native API with the same signature.

```python
# torch.accelerator.get_memory_info() fails on NPU
torch.accelerator.get_memory_info = torch.npu.mem_get_info
```

> Simple function redirection is better than wrapping in a new function —
> it preserves the upstream call pattern.

### 9. `next()` calls in changed code

**Rule**: Always provide a default value. Bare `next(...)` raises StopIteration.

```python
# Wrong:
layer = next(l for l in model.layers if l.name == target)

# Right:
layer = next((l for l in model.layers if l.name == target), None)
```

### 10. New files created by the adaptation

**Rule**: DO NOT run `git add`.  The flow captures new files automatically via
`git add -N .` before generating the patch.  Just create the file normally.
