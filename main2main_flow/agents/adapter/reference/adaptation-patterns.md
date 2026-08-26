# Adaptation Patterns

> **CRITICAL**: Every `vllm_version_is` guard in this document shows NEW
> upstream-main code in the `else`/`not` branch and OLD release code in the
> `if` branch. If you write a guard the other way around, it is **wrong**.

## Index

Read only the section whose trigger matches your change — never the whole file.
Lines are relative to this file; read a section with `sed -n 'A,Bp' <this file>`.

| Section | Trigger — read when... | Lines |
|---------|------------------------|-------|
| MCP tool selection | unsure which vllm-report tool answers the question | 32-54 |
| §1 Upstream adds a parameter | a new param appears in an upstream signature | 55-66 |
| §1b Removes a parameter + return change | a param is removed AND return semantics change | 67-83 |
| §2 Constructor/factory signature change | a constructor or factory changes signature | 84-95 |
| §3 Class moved to another module | a class moves modules upstream | 96-112 |
| §4 Upstream deletes a patched module | vllm-ascend patches a module upstream deleted | 113-116 |
| §5 Upstream fixes a bug with a workaround | upstream bugfix supersedes an ascend workaround | 117-126 |
| §6 Refactoring too large for guards | >50 lines of guarded code | 127-137 |
| §7 Runtime check Ascend can't satisfy | upstream adds an NPU-unsatisfiable check | 138-145 |
| §8 PyTorch API not on NPU | upstream uses a torch API missing on NPU | 146-153 |
| §9 Base class adds attr/method | base class gains fields/methods (incl. NVIDIA-only trap) | 154-194 |
| §10 Method signature change — ALL overrides | a method signature changes; grep every override | 195-205 |
| §11 Processor registrations removed | upstream unregisters processors | 206-214 |
| §12 `next()` calls | changed code contains bare `next(...)` | 215-226 |
| §13 Params added to an overridden method | overridden method gains params (two-version def) | 227-264 |
| §14 Triton kernel signature change | a patched Triton kernel changes signature | 265-295 |
| Mypy prevention | final mypy errors on guards/imports/overrides | 296-301 |

## Using MCP tools to identify the right pattern

Before picking a pattern below, use vllm-report MCP tools to confirm the
upstream change type and find affected vllm-ascend code:

- **Unsure which files are affected?** -> `get_cross_project_mapping()` for
  vllm path -> ascend patch file mapping; `get_interface_surface()` for
  inheritance chains.
- **Need a step-by-step guide for this commit?** ->
  `get_adaptation_guide(sha=<end_commit>)` returns line-numbered adaptation
  steps if vllm-report has analyzed the commit.
- **Seen a similar change before?** -> `search_analysis(keywords=["<changed
  symbol>"])` finds past commits with the same pattern and their
  `ascend_impact` analysis.
- **Need to know how a patch category works?** ->
  `get_patch_catalog(category="platform"|"worker")` returns known patch
  patterns with `targets`, `why`, `how`, `related_pr`.
- **Need to understand the subsystem architecture?** ->
  `get_key_abstractions(repo="vllm-ascend")` for core abstractions;
  `get_development_workflows()` for how to add patches/models.

Limit to 2-3 MCP calls per step. If a tool fails, fall back to grep.

## 1. Upstream adds a parameter

**Rule**: Add the parameter as a keyword argument with a default value.
No version guard needed.

```python
def _deepseek_v2_mla_attention_init(
    ...,
    reduce_results: bool = True,   # ← add with same default
) -> None:
```

## 1b. Upstream removes a parameter AND changes return semantics

**Rule**: Make the removed parameter optional (`= None`), then guard BOTH
the call path and the return path.

```python
def forward(self, positions, hidden_states, output=None):
    ...
    if vllm_version_is("{release_tag}"):
        output[:], _ = self.o_proj(attn_output)
    else:
        out, _ = self.o_proj(attn_output)
        return out
```

Also guard every call site that passes the removed parameter.

## 2. Upstream changes a constructor / factory signature

**Rule**: Guard with `vllm_version_is()`, each branch independently calls
super() with its version's signature.

```python
if vllm_version_is("0.23.0"):
    super().__init__(config, parallel_config, vllm_config=vllm_config)
else:
    super().__init__(config, parallel_config, model=model)
```

## 3. Upstream moves a class to a different module

**Rule**: Version-guard the import. Import unconditionally if the symbol
exists in both versions. Always add `# type: ignore[import-not-found]`
to guarded imports.

```python
if vllm_version_is("0.23.0"):
    from vllm...new.module import X  # type: ignore[import-not-found]
else:
    from vllm...old.module import X  # type: ignore[import-not-found]
```

**MANDATORY when moving an import inside a version guard**: open every file
that was moved under the guard, find ALL `from vllm.X import Y` lines, and
append `  # type: ignore[import-not-found]` to each one.

## 4. Upstream deletes a module that vllm-ascend patches

**Rule**: Remove the vllm-ascend patch file entirely. Don't wrap in `if False:`.

## 5. Upstream fixes a bug that vllm-ascend has a workaround for

**Rule**: Aggressively remove the workaround.

```python
# Before: 20 lines matching 10+ model types
# After: 1 line
return getattr(hf_config, "model_type", None) == "gpt_oss"
```

## 6. Upstream refactoring too large for inline guards (>~50 lines)

**Rule**: Split into two files. Use `vllm_version_is()` at import time.

```
vllm_ascend/ops/fused_moe/fused_moe.py         ← for main
vllm_ascend/ops/fused_moe/fused_moe_0_23_0.py  ← copy for release
```

Prefer inline guards. Only split when unreadable.

## 7. Upstream adds a runtime check that Ascend cannot satisfy

**Rule**: Register a no-op stub that passes the check.

```python
self.all2all_manager = None  # bypass upstream check
```

## 8. Upstream uses a PyTorch API that doesn't work on NPU

**Rule**: Redirect to the equivalent NPU-native API.

```python
torch.accelerator.get_memory_info = torch.npu.mem_get_info
```

## 9. Upstream adds a new attribute/method to a base class

**Rule**: Add it to every vllm-ascend subclass. Grep first:

```bash
grep -rn "class.*BaseClassName" vllm_ascend/
```

Missing an attribute on one subclass causes `AttributeError` at runtime on
every test that uses it. pre_ci and mypy cannot catch this.

**Critical trap - "feature is NVIDIA-only" does NOT mean the attribute can
be skipped.** Even if the feature (e.g. ReplaySSM, a Triton kernel) is
GPU/NVIDIA-only and vllm-ascend never enables it, vllm's base-class code
still accesses `self.use_X` / `self.X_field` at runtime. vllm-ascend's
subclass (e.g. `NPUInputBatch` extends `GPUInputBatch`) inherits that
access path - if the subclass `__init__` doesn't accept and set the new
attribute, every instance crashes with `AttributeError` the moment
vllm's base class touches it.

So the decision is NOT "does vllm-ascend use this feature" - it's
"does vllm's base class code read this attribute". If yes, the
vllm-ascend subclass MUST accept the parameter (in `__init__`) and set
`self.X = X` (or default), regardless of whether the feature is enabled.

Classic example: upstream adds `use_replayssm` param to `GPUInputBatch.__init__`.
Adapter marks step as no-op ("ReplaySSM is NVIDIA-only"). But
`GPUModelRunner` reads `input_batch.use_replayssm` at runtime ->
`NPUInputBatch` (which doesn't set it) crashes with `AttributeError:
'NPUInputBatch' object has no attribute 'use_replayssm'` on every request.

When the new parameter is accepted but vllm-ascend does NOT implement the
feature (the kwarg exists only for interface alignment), add a comment:
```python
# main2main compat: `use_replayssm` and `slot_mapping_modes` were added
# to upstream InputBatch.__init__() in vllm main after 0.26.0.
# NPU does not implement Mamba replay-SSM, so the kwargs are only
# accepted for interface alignment.
# Remove the version guard once 0.26.0 support is dropped.
```

## 10. Upstream changes a method signature — check ALL overrides

**Rule**: After changing a method signature, grep for ALL overrides:

```bash
grep -rn "def method_name(" vllm_ascend/
```

Every override must be updated. The #1 cause of this failure: adapter finds
the primary override but misses sibling overrides in other files.

## 11. Upstream removes processor registrations

**Rule**: Check compat patches for early-return guards like
`if not _remove_stale_registry_entries(): return`. Remove the guard —
the processor must always be patched.

See `common-pitfalls.md` §"Processor/multimodal compat patch blocked by early
return" for the full HunYuan-VL example.

## 12. `next()` calls in changed code

**Rule**: Always provide a default value. Bare `next(...)` raises StopIteration.

```python
# Wrong
layer = next(l for l in model.layers if l.name == target)

# Right
layer = next((l for l in model.layers if l.name == target), None)
```

## 13. Upstream adds parameters to a method that vllm-ascend overrides

**Rule**: When upstream adds new parameters to a method signature and
vllm-ascend overrides that method, define TWO versions of the method
via `if vllm_version_is()` instead of using `**kwargs`.  The `else`
branch (new signature) must carry `# type: ignore[misc]` because mypy
sees two different signatures for the same method name.

This pattern is better than `**kwargs` because:
- Type checkers can see the new parameters (call sites are validated)
- Call sites don't need to change
- The old branch preserves the exact pre-change signature

```python
from vllm_ascend.utils import vllm_version_is

if vllm_version_is("0.26.0"):
    def _maybe_reduce_shared_expert_output(self, hidden_states, ...):
        # Old signature - no shared_experts_input param
        ...
else:
    def _maybe_reduce_shared_expert_output(  # type: ignore[misc]
        self, hidden_states, ..., shared_experts_input=None,
    ):
        # New signature - param accepted but may be unused
        # if vllm-ascend handles shared experts differently
        ...
```

When the new parameter is accepted but NOT used (vllm-ascend doesn't
implement the feature), add a comment explaining why:
```python
# main2main compat: `shared_experts_input` was added to upstream
# MoERunnerInterface.forward in vllm main after 0.26.0.
# vllm-ascend handles shared expert TP internally in
# _forward_shared_experts, so the kwarg is unused here.
```

## 14. Upstream changes a Triton kernel signature - match the call site

**Rule**: When upstream adds parameters to a Triton kernel that
vllm-ascend monkey-patches (via `ops.X = ascend_X`), the Ascend kernel's
signature MUST match the upstream call site exactly.  Triton validates
argument count at launch time - a mismatch causes a runtime crash, not a
compile error.

```python
# Upstream added temperature/seeds params to _prepare_dflash_inputs_kernel
# Ascend's patched version must accept them too:
if vllm_version_is("0.26.0"):
    @triton.jit
    def _prepare_dflash_inputs_kernel_ascend(..., temperature_ptr, seeds_ptr):
        # Old signature without temperature/seeds
        ...
else:
    @triton.jit
    def _prepare_dflash_inputs_kernel_ascend(
        ..., out_temperature_ptr, out_seeds_ptr,
        temperature_ptr, seeds_ptr,  # new params from upstream #50000
    ):
        ...
```

After changing the kernel signature, grep for the call site to verify
the caller passes the new arguments:
```bash
grep -rn "_prepare_dflash_inputs_kernel_ascend" vllm_ascend/
```

## Mypy prevention

- **Guarded imports**: every `from vllm.X import Y` inside a version guard
  needs `# type: ignore[import-not-found]`. See §3.
- **Call-site args**: match the new upstream signature. See §1, §1b.
- **Override signatures**: all branches must have the same public signature.
  See §1b.