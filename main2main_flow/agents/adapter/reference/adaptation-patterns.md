# Adaptation Patterns

> **CRITICAL**: Every `vllm_version_is` guard in this document shows NEW
> upstream-main code in the `else`/`not` branch and OLD release code in the
> `if` branch. If you write a guard the other way around, it is **wrong**.

Patterns extracted from successful main2main PRs.

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

## Mypy prevention

- **Guarded imports**: every `from vllm.X import Y` inside a version guard
  needs `# type: ignore[import-not-found]`. See §3.
- **Call-site args**: match the new upstream signature. See §1, §1b.
- **Override signatures**: all branches must have the same public signature.
  See §1b.