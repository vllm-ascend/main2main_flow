# Adaptation Patterns

Patterns extracted from successful main2main PRs. For each upstream change type
a correct adaptation approach and anti-pattern.

## 1. Upstream adds a parameter to a function vllm-ascend calls or overrides

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

## 2. Upstream changes a constructor / factory signature

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

## 3. Upstream moves a class to a different module

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

When an import is version-guarded and the module only exists in some versions,
**always** add `# type: ignore[import-not-found]` to the import line.  mypy
checks all code paths regardless of runtime guards; without the ignore
comment, it reports a false positive on the version where the module is absent.

```python
if vllm_version_is("0.23.0"):
    from vllm.tool_parsers.deepseekv4_tool_parser import DeepSeekV4ToolParser  # type: ignore[import-not-found]
```

**MANDATORY when moving an existing `import` inside a version guard**:

1. Open every file that was moved under the guard.
2. Find ALL `from vllm.X import Y` lines in those files.
3. Append `  # type: ignore[import-not-found]` to each one.
4. Do NOT leave any un-ignored vllm import in a guarded file.

mypy checks every `.py` file regardless of runtime guards.  A single
un-ignored import in a guarded file breaks CI.  This is not optional.

## 4. Upstream deletes a long-standing module that vllm-ascend patches

**Rule**: Remove the vllm-ascend patch file entirely.  A patch against a
deleted upstream module is dead code — deleting it is the correct adaptation.

> Anti-pattern: wrapping the entire patch file body in `if False:` or a
> never-satisfied guard.  Just delete it. The step summary explains why.

## 5. Upstream fixes a bug that vllm-ascend has a workaround for

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

## 6. Upstream refactoring is too large for inline guards (>~50 lines diff)

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

## 7. Upstream adds a runtime check that Ascend cannot satisfy

**Rule**: Register a no-op stub that passes the check.  Annotate with "this is
not a functional implementation — it exists only to satisfy the upstream guard."

```python
# Upstream MoE init checks `all2all_manager is not None`
# Ascend doesn't use all2all — register a dummy to bypass the check:
self.all2all_manager = None  # no-op: bypass upstream MoE fault-tolerance check
```

> The stub must have the expected type so attribute access doesn't raise.

## 8. Upstream uses a PyTorch API that doesn't work on NPU

**Rule**: Redirect to the equivalent NPU-native API with the same signature.

```python
# torch.accelerator.get_memory_info() fails on NPU
torch.accelerator.get_memory_info = torch.npu.mem_get_info
```

> Simple function redirection is better than wrapping in a new function —
> it preserves the upstream call pattern.

## 9. `next()` calls in changed code

**Rule**: Always provide a default value. Bare `next(...)` raises StopIteration.

```python
# Wrong:
layer = next(l for l in model.layers if l.name == target)

# Right:
layer = next((l for l in model.layers if l.name == target), None)
```

## 10. New files created by the adaptation

**Rule**: DO NOT run `git add`.  The flow captures new files automatically via
`git add -N .` before generating the patch.  Just create the file normally.
