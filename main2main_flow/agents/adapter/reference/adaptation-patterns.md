# Adaptation Patterns

> **CRITICAL**: Every `vllm_version_is` guard in this document shows NEW
> upstream-main code in the `else`/`not` branch and OLD release code in the
> `if` branch.  If you write a guard the other way around, it is **wrong**.
> This single mistake is the #1 cause of failed main2main PRs.
> `common-pitfalls.md` §"Version guard direction is inverted" has the
> mandatory self-check and real failure examples.

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

## 1b. Upstream removes a parameter AND changes return semantics

**Rule**: Make the removed parameter optional (`= None`), then guard the
*both* the call path and the return path with `vllm_version_is()`.

This is the inverse of §1 and is easy to get wrong.  The trap: it looks
like a simple signature change, but the upstream also changed how results
are returned (write-to-output-buffer → return-value).

```python
# Upstream removed `output` parameter and now returns the result:
#   Old call: self.linear_attn(hidden_states=hidden_states, output=buf)
#   New call: hidden_states = self.linear_attn(hidden_states=hidden_states)

# Wrong: just redirect a forward method without touching the call pattern.
# This misses the signature change at the call site.
_GDN_PATCH_TARGET.forward = AscendGatedDeltaNetAttention.forward

# Right: make the parameter optional, guard both input and output.
def forward(self, positions, hidden_states, output=None):
    qkv, _ = self.qkv_proj(hidden_states)
    ...
    if vllm_version_is("{release_tag}"):
        output[:], _ = self.o_proj(attn_output)      # old: write to buffer
    else:
        out, _ = self.o_proj(attn_output)            # new: return value
        return out
```

Then at every call site, guard the calling pattern too:
```python
if vllm_version_is("{release_tag}"):
    self.linear_attn(hidden_states=hidden_states, output=self_attention_output)
    hidden_states = self_attention_output
else:
    hidden_states = self.linear_attn(hidden_states=hidden_states)
```

> Anti-pattern: adding only `_GDN_PATCH_TARGET.forward = ...` without touching
> the method signature or the call sites.  This works for 310P-only paths but
> silently breaks the regular path where the upstream now calls differently.

> If the removed parameter was a mutable output buffer (`output[:] = result` →
> `return result`), the return-type semantics changed too — guard that.

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

## 11. Prevent mypy errors before CI

mypy runs in CI on every file regardless of runtime guards.  The following
checks prevent the most common mypy failures.  CI runs these automatically;
apply them proactively so the PR passes first time.

### 11.1 Version-guarded imports → type: ignore[import-not-found]

Every `from vllm.X import Y` inside a `vllm_version_is` guard must have
`# type: ignore[import-not-found]`.  See §3 for the full rule.

### 11.2 Call-site argument changes → match the new signature

When upstream adds/removes/renames a parameter, every vllm-ascend call site
that passes arguments to that function must match:

```python
# Wrong: upstream added `reduce_results: bool = True`
super().__init__(config, prefix, topk_indices_buffer)

# Right: match the new signature
super().__init__(config, prefix, topk_indices_buffer, reduce_results=True)
```

mypy error: `[call-arg]` — unexpected keyword argument / missing argument.

### 11.3 Attribute/method moves → access from the new location

When upstream moves a config field or method to a different class, every
vllm-ascend access must use the new location.  Use `vllm_version_is()` to
guard between old and new:

```python
if vllm_version_is("0.23.0"):
    value = obj.new_location.field_name
else:
    value = obj.old_location.field_name
```

mypy error: `[attr-defined]` — object has no attribute "X".

### 11.4 Override signature mismatch → keep it identical

When overriding an upstream method, the signature must match exactly.
All branches of a version guard must have the same public signature.

```python
# Wrong: branches have different signatures
if vllm_version_is("0.23.0"):
    def forward(self, x, y=None): ...    # 2 params
else:
    def forward(self, x, y=None, z=None): ...  # 3 params — mypy error

# Right: same signature on every branch
def forward(self, x, y=None, z=None):
    if vllm_version_is("0.23.0"):
        ...  # z is unused in this branch
    else:
        ...
```

mypy error: `[override]` — signature incompatible with superclass.

## 12. Upstream removes processor registrations from `_CLASS_TO_MODULE`

**Rule**: When upstream cleans up `_CLASS_TO_MODULE` in
`transformers_utils/processors/__init__.py` (removing entries like
`HunYuanVLProcessor`), check vllm-ascend for compat patches that have
an `install_*` function.  Look for early-return guards like:

```python
if not _remove_stale_registry_entries():
    return  # ← remove this guard — the processor must still be patched!
```

The guard incorrectly assumes that "no stale entries to remove" means
"nothing to do." But the compat processor must still be installed even
when upstream already cleaned the registry.  Remove the guard and
always patch the processor loader.

Also check whether upstream's `_call_hf_processor` now accesses new
tokenizer attributes (e.g. `hf_processor.image_token`).  If so, the
compat code's `get_hf_processor` patch needs to register those tokens
on the tokenizer BEFORE constructing the processor — the processor's
`__init__` is too late because Transformers validates the tokenizer
first.  Use `getattr(self.ctx, "tokenizer", None)` to access the
tokenizer early.

See `common-pitfalls.md` §"Processor/multimodal compat patch blocked by
early return" for the concrete HunYuan-VL example.
