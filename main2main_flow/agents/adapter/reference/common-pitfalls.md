# Common Pitfalls

Mistakes that break CI or cause silent failures.

## Version guard direction is inverted

**Symptom**: New upstream-main behavior runs on the release version instead, or
old behavior runs on main.  This is the #1 cause of failed main2main PRs.

**Prevention — MANDATORY self-check before every `vllm_version_is` guard**:

1. "Is this guard protecting NEW upstream-main behavior or OLD release behavior?"
2. If NEW behavior: "Is it in the `else` or `not vllm_version_is` branch?" — it **must** be
3. If OLD behavior: "Is it in the `if vllm_version_is` branch or absent?" — it **must** be

**Failure cascade when guards are inverted** (real example from PR #12519):

When `if vllm_version_is("0.25.1")` guards are used for NEW code instead of `else`:

| Guard type | What breaks | Where | Error |
|-----------|------------|-------|-------|
| New param in `if` (should be `else`) | Release (old vllm) | `SingleTypeKVCacheManager.__init__` | `TypeError: got unexpected keyword argument` |
| New return type in `if` (should be `else`) | Main (new vllm) | `find_longest_cache_hit` call sites | `ValueError: not enough values to unpack` |
| Renamed attr in `if` (should be `else`) | Main (new vllm) | `cache_config.hash_block_size` → `prefix_match_unit` | `AttributeError: no attribute` |
| New positional arg in `if` | Release (old vllm) | `get_num_blocks_to_allocate` call sites | `TypeError: missing required positional` |

> Always add new parameters as **keyword** args with defaults — never as
> positional-only, or they will shift the positional positions and break
> callers on the release version.

> If both branches of a guard are version-dependent, add `# type: ignore`
> comments to each branch so mypy does not flag the branch that doesn't
> apply to the current version.

## Importing modules that don't exist (yet)

**Symptom**: mypy `import-not-found` for a `from vllm.X import Y` line inside
a `vllm_version_is` guard. mypy checks all static code paths regardless of
runtime guards.

**Prevention**: Add `# type: ignore[import-not-found]` to every import that
only exists in some vllm versions:

```python
from vllm.X import Y  # type: ignore[import-not-found]
```

Before writing any unconditional `from vllm.X import Y`, verify the module
exists:
```bash
find ${VLLM_DIR}/vllm -name "X.py"
```

## Indentation errors

When inserting version-guard blocks into existing code, **count the leading
spaces** of surrounding lines in the same block. Copy the count exactly.
Do not eyeball it.

## Variable aliases as base classes

**Symptom**: mypy `[valid-type]` and `[misc]` on `class X(_Base):` where
`_Base = SomeClass` is a variable assignment. `# type: ignore[name-defined]`
does NOT suppress `valid-type` or `misc`.

**Prevention**: Use the class directly, or `TypeAlias`:

```python
# Wrong
_PrefillBase = Manager
class X(_PrefillBase):  # [valid-type] [misc]

# Right
from typing import TypeAlias
_PrefillBase: TypeAlias = Manager
class X(_PrefillBase):
```

**ALSO**: If the imported class was added only on main, the import MUST be
version-guarded inside `else`.

## Missing attribute on subclass after upstream adds one

**Symptom**: `AttributeError: 'AscendX' object has no attribute 'Y'` at
runtime. Upstream added a new field/method to a base class that vllm-ascend
subclasses, and the adapter didn't add it to the subclass.

**Prevention**: When upstream adds a new attribute, method, or config field
to a class that vllm-ascend subclasses or wraps, grep for ALL subclasses:

```bash
grep -rn "class.*BaseClassName" vllm_ascend/
```

Add the new attribute to every subclass. Even if it's just a default value,
without it the subclass will `AttributeError` at runtime.

## Missing override in sibling class after method signature change

**Symptom**: `TypeError: missing required positional argument` on a class
that wasn't directly modified. Upstream changed a method signature, the
adapter updated one subclass but missed another subclass in a different file.

**Prevention**: When changing a method signature in one class, grep for ALL
definitions of that method name in the entire vllm-ascend tree:

```bash
grep -rn "def method_name(" vllm_ascend/
```

Every override found must have the updated signature. Fixing only one
subclass while leaving others broken is the most expensive mistake (all CI
jobs fail).

## Positional argument order after upstream inserts a parameter

When upstream inserts a new parameter between existing ones (not at the end),
positional callers in the `else` branch must match the new order. Passing the
right number of arguments in the wrong order is a silent bug.

```python
# Upstream: get_num_blocks(..., total_computed, num_local, num_main)
# Old:      get_num_blocks(..., total_computed, num_main)

# Wrong: right count, wrong order
self.get_num_blocks(..., total, num_main, num_local)

# Right: matches new upstream order
self.get_num_blocks(..., total, num_local, num_main)
```

**Prevention**: Always use keyword arguments for new parameters in the
`else`/`not` branch.

## hit_length computation with wrong block_size

**Symptom**: v0.25.1-only native crash (segfault, no Python traceback) on MLA
models (DeepSeek-V2-Lite/V3/V4). Main passes.

**Root cause**: When upstream changes `find_longest_cache_hit` return type so
that v0.25.1 must compute `hit_length` externally, the computation must use
the **physical** block size (`spec.block_size`), not `_get_effective_block_size()`
which multiplies by `compress_ratio` for MLA specs.

```python
# Wrong: effective_block_size includes compress_ratio -> 4x over-count
_new_hit_length = len(hit_blocks[0]) * effective_block_size

# Right: use physical block_size
_new_hit_length = len(hit_blocks[0]) * spec.block_size
```

**Check ALL sibling functions**: this bug pattern always spreads to sibling
functions in the same file. After fixing one, grep and fix all:

```bash
grep -n 'effective_block_size\|len(hit_blocks\[0\])' vllm_ascend/patch/platform/patch_kv_cache_coordinator.py
```

## Processor/multimodal compat patch blocked by early return

**Symptom**: After upstream removes processor registrations from
`_CLASS_TO_MODULE`, model test fails with `Tokenizer is missing required
attribute 'image_token'`.

**Root cause**: vllm-ascend compat patch has an `install_*` function with:

```python
if not _remove_stale_registry_entries():
    return   # ← BUG: skips processor patching when registry is already clean!
```

Upstream already cleaned the registry, so the function returns early and the
compat processor never gets installed. Remove the `if not ...: return` guard.

**Also**: When upstream's `_call_hf_processor` accesses new tokenizer
attributes (e.g. `hf_processor.image_token`), register those tokens on the
tokenizer BEFORE calling `ctx.get_hf_processor()` — use
`getattr(self.ctx, "tokenizer", None)` to access the tokenizer early.

## Patching symbols from deleted upstream modules

**Symptom**: A patch file patches `vllm.X.Y` but `vllm.X` was deleted upstream.

**Prevention**: Check before patching:
```bash
test -f "${VLLM_DIR}/vllm/X.py" || echo "MODULE DELETED"
```
If deleted, remove the patch file. See `adaptation-patterns.md` §4.

## `hasattr` / `try-except` used instead of version guard

**Symptom**: Code uses `if hasattr(obj, "new_field")` or `try: import X except:
pass` instead of `vllm_version_is()`. These pass pre_ci but break when the
upstream type changes.

**Prevention**: Always use `vllm_version_is("{release_tag}")` for version
boundaries. `hasattr`/`try-except` silently mask the wrong kind of change.

## Output-buffer trap

When upstream changes from `output[:] = result` to `return result`, don't
just add a forward redirect. Make the removed parameter optional, guard
the return path, and guard every call site. See `adaptation-patterns.md` §1b.

## Format violations (E501 / F821 / F841 / F401)

| Code | Meaning | Prevention |
|------|---------|------------|
| E501 | Line too long (>120) | Break line before committing |
| F821 | Undefined name | Add missing import |
| F841 | Unused variable | Remove or prefix with `_` |
| F401 | Unused import | Delete the import line |
| I001 | Unsorted imports | Sort manually |

Ruff format CANNOT auto-fix E501/F821/F841 — these need manual code edits.

## Common typos that break codespell / typos

| Wrong | Right |
|-------|-------|
| `unparseable` | `unparsable` |

Check every added comment and string for spelling.

## Additional QA-level checks

These are caught by the QA reviewer but should be applied proactively:

- **`next()` must have a default value**: always `next(iter, default)`, never bare `next(...)`.
- **`super().__init__()` must be called**: every subclass `__init__` must chain to parent.
- **Verify registries after touching KVCacheSpecRegistry**: when removing old-version
  branches, confirm `KVCacheSpecRegistry.register()` / `__init_subclass__` calls are not
  deleted. Grep: `grep -rn "register\|__init_subclass__" vllm_ascend/` near changed code.
- **No exact version matching**: never `== "X.Y.Z"`. Use `vllm_version_is()` or `>=`.
- **Grep before deleting**: before removing any function/env-var/utility, grep the full
  call chain. Even if a function appears single-version, multiple patch files may depend
  on it.
- **`getattr` for cross-version params**: when an upstream parameter changes type across
  versions, `getattr(obj, "param", default)` is acceptable. This is different from using
  `hasattr`/`try-except` FOR VERSION DETECTION, which is prohibited.
- **Triton kernel params must match**: every arg passed to a Triton kernel call must exist
  in the kernel function signature.
- **No `logging.debug` on TorchDynamo compile path**: guard with
  `if not torch.compiler.is_compiling()`.
- **Resolve paths before chaining**: call `.resolve()` before `.parents[N]` on `Path`.
- **Remove dead code**: commented-out experimental lines, `# "FusedMoE": AscendFusedMoE,`
  blocks left for reference — remove them.
- **Clean up stale `# type: ignore`**: when editing nearby code, remove redundant
  type-ignore comments and meaningless annotations.
- **Document default value changes**: when changing a parameter's default (e.g.
  `swiglu_limit: 0 → None`), explain the reason in step_summary.md.

## Fix mode workflow

1. Extract search term from error (method name, config field, class name)
2. Search upstream patch (`{patch_path}`) for that term
3. Identify upstream intent: rename, removal, new parameter, new method
4. Map to vllm-ascend code that depends on it
5. Decide if `vllm_version_is` guard is needed
6. Open pre_ci_check.json → read violations → fix each file:line:col:CODE
7. Run `bash format.sh` → repeat until clean