# Common Pitfalls

Mistakes that break CI or cause silent failures.

## Index

Read only the section whose symptom matches your failure — never the whole file.
Lines are relative to this file; read a section with `sed -n 'A,Bp' <this file>`.

| Section | Trigger — read when... | Lines |
|---------|------------------------|-------|
| Version guard direction is inverted | any `vllm_version_is` guard you write (self-check) | 35-64 |
| Importing modules that don't exist | mypy `import-not-found` on guarded imports | 65-83 |
| Indentation errors | inserting guard blocks into existing code | 84-89 |
| Variable aliases as base classes | mypy `[valid-type]` / `[misc]` on `class X(_Base)` | 90-111 |
| Missing attribute on subclass | `AttributeError: no attribute 'Y'` after upstream adds a field | 112-121 |
| Return type mismatch across branches | release-only `AttributeError` on a return value, main passes | 122-142 |
| Missing override in sibling class | `TypeError: missing required positional` on an unmodified class | 143-159 |
| Positional argument order | upstream inserts a param between existing ones | 160-179 |
| hit_length computation with wrong block_size | v0.25.1 MLA crash (segfault, no traceback) | 180-204 |
| Processor patch blocked by early return | `Tokenizer is missing required attribute 'image_token'` | 205-225 |
| Fix covers only ONE of multiple paths | the SAME e2e failure recurs after your fix (HunyuanVL) | 226-288 |
| Patching symbols from deleted modules | a patch file references a deleted `vllm.X` | 289-298 |
| `hasattr` / `try-except` instead of guard | code uses `hasattr` or `try: import` for version detection | 299-304 |
| Output-buffer trap | upstream changes `output[:] = result` to `return result` | 305-310 |
| Format violations (E501/F821/F841/F401) | pre_ci or gate flags a format code | 311-322 |
| Common typos (codespell) | gate flags spelling | 323-330 |
| Additional QA-level checks | proactive checklist before submitting (next(), super(), registries) | 331-357 |
| Environment compatibility stubs | `ImportError` from a pinned dep (triton etc.) at import time | 358-402 |
| triton-ascend kernel constraints | porting a Triton kernel; `CompilationError`/`KeyError` at launch | 403-452 |
| `device_index` must be explicit | NPU device APIs in version-guarded branches | 453-474 |
| Variable name shadowing | `AttributeError`/wrong-type errors at a call site | 475-498 |
| mypy error codes (final gate) | final quality gate mypy failures, fix per `[code]` | 499-530 |

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

**Symptom**: `AttributeError: 'AscendX' object has no attribute 'Y'` when
upstream adds a field to a base class vllm-ascend subclasses. Add it to
EVERY subclass — see `adaptation-patterns.md` §9 for the full rule:

```bash
grep -rn "class.*BaseClassName" vllm_ascend/
```

## Return type mismatch across version branches

**Symptom**: `AttributeError: 'list' object has no attribute 'ref_cnt'` at
runtime on the release version. Main passes. No mypy error.

**Root cause**: Upstream changed a method's return type (e.g.
`tuple[list, ...]` → `tuple[tuple[list, ...], int]`). Adapter updated the
type annotation and the main-branch return statements, but a version-guarded
`return old_list` inside the `if vllm_version_is` branch still returns the
old type. mypy doesn't catch this because the annotation says the new type.

**Prevention**: After updating any method's return type, grep ALL `return`
statements inside that method:

```bash
grep -n "return " vllm_ascend/path/to/file.py
```

Verify every branch returns the new type. Don't rely on the type annotation
alone — it can be wrong without mypy noticing.

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

## Fix covers only ONE of multiple code paths (RECURRING — HunyuanVL case)

**Symptom**: E2E fails with an assertion/error from an upstream processor
or patch helper. The SAME failure recurs across runs — your first fix
looks correct (you fixed the function the traceback names) but E2E still
fails.

**Root cause (the trap)**: upstream code often has MULTIPLE paths that
reach the same invariant. You fixed one path; the failing path is
another. Common path splits:

- **Normal vs cache**: upstream caches tokenization/processing results
  (e.g. `_cached_apply_hf_processor`). The cache path reuses a previous
  result and SKIPS the normal-path code your patch lives in. A patch
  that only hooks the normal path misses every cached call.
- **With-data vs no-data**: a processor wraps placeholders only when
  data is present (`if mm_data.get("images") is not None:`). The
  text-only / no-data path never gets the wrapping → downstream match
  asserts.
- **Multimodal vs single-modality, batch vs single, prefetch vs lazy**:
  any condition that branches around your patched function is a
  potential missed path.

**Diagnosis — ask BEFORE fixing**:
1. Read the FULL traceback. Which path was executing? (cache?
   text-only? no-data? a different module than the assert line?)
2. Does the patched function get CALLED on that path? If not, your
   patch there is useless for this failure.
3. Grep for every call site / branch that reaches the invariant the
   traceback asserts. Each one needs the fix.

**Fix requirements — check ALL paths, not just one**:
1. Patch the function at the deepest common point both paths reach —
   or patch EACH path separately if they don't share code.
2. For cache paths: invalidate or update the cache when the patch
   changes behavior, or apply the transformation at the point where
   both cached and fresh results flow through.
3. Version guards must cover the failing branch on the target version,
   not just the version you tested.
4. Verify by re-reading the E2E test: what input does it send, and
   which path does that input take?

**Concrete case — HunyuanVL prompt replacement (PR #13657, main2main
08-05/08-06)**: `test_vlm.py::test_multimodal_vl[hunyuan-vl]` failed
3 runs in a row with `AssertionError: Failed to apply prompt replacement
for mm_items['image'][0]`. On vllm main, `_get_prompt_updates` targets
the 3-token sequence `[image_start, image_token, image_end]`
(`[120118, 120120, 120119]`). Two paths:
- with-image: `_call_hf_processor` wraps the bare placeholder when
  `mm_data.get("images")` is not None → compat patch works.
- text-only cache path: the prompt is tokenized without images, the
  bare `<no_102>` is NEVER wrapped → assert. Main's native wrapping
  misses this path entirely.

Fix points (all three): compat patch must wrap in BOTH paths (test the
cache path explicitly); the test prompt (`hunyuan_prompt` in
`tests/e2e/conftest.py`) must supply the wrapped placeholder on main;
version guard must cover main's cache path (0.26.0 target was the bare
token, main is wrapped).

**When this error message appears, do NOT re-diagnose from scratch** —
apply the multi-path checklist above.

## Patching symbols from deleted upstream modules

**Symptom**: A patch file patches `vllm.X.Y` but `vllm.X` was deleted upstream.

**Prevention**: Check before patching:
```bash
test -f "${VLLM_DIR}/vllm/X.py" || echo "MODULE DELETED"
```
If deleted, remove the patch file. See `adaptation-patterns.md` §4.

## `hasattr` / `try-except` used instead of version guard

**Symptom**: Code uses `if hasattr(obj, "new_field")` or `try: import X except:
pass` for version detection. These pass pre_ci but silently break when the
upstream type changes — always use `vllm_version_is()` instead.

## Output-buffer trap

When upstream changes `output[:] = result` to `return result`, make the
removed parameter optional, guard the return path, and guard every call
site. See `adaptation-patterns.md` §1b.

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

These are caught by the QA reviewer but should be applied proactively
(the `next()` / `super().__init__()` / no-exact-version / dead-code items
already live in the SKILL.md verify checklist):

- **Verify registries after touching KVCacheSpecRegistry**: when removing old-version
  branches, confirm `KVCacheSpecRegistry.register()` / `__init_subclass__` calls are not
  deleted. Grep: `grep -rn "register\|__init_subclass__" vllm_ascend/` near changed code.
- **Grep before deleting**: before removing any function/env-var/utility, grep the full
  call chain. Even if a function appears single-version, multiple patch files may depend
  on it.
- **`getattr` for cross-version params**: when an upstream parameter changes type across
  versions, `getattr(obj, "param", default)` is acceptable. This is different from using
  `hasattr`/`try-except` FOR VERSION DETECTION, which is prohibited.
- **Triton kernel params must match**: every arg passed to a Triton kernel call must exist
  in the kernel function signature. When syncing an NPU Triton kernel, also apply the
  triton-ascend authoring constraints (boolean chains / int64 / uint64 / new constexpr
  params) — see the "triton-ascend kernel authoring constraints" section below.
- **No `logging.debug` on TorchDynamo compile path**: guard with
  `if not torch.compiler.is_compiling()`.
- **Resolve paths before chaining**: call `.resolve()` before `.parents[N]` on `Path`.
- **Clean up stale `# type: ignore`**: when editing nearby code, remove redundant
  type-ignore comments and meaningless annotations.
- **Document default value changes**: when changing a parameter's default (e.g.
  `swiglu_limit: 0 → None`), explain the reason in step_summary.md.

## Environment compatibility stubs (vllm main vs pinned deps)

vllm-ascend pins dependencies (e.g. `triton-ascend==3.2.1`) that lag behind
vllm main. When vllm main adds an import that the pinned dep can't satisfy,
E2E tests fail with `ImportError` at vllm import time. **This is NOT an env
flake to skip** - it is a real adaptation gap. The standard fix is a compat
stub in `vllm_ascend/__init__.py` (runs at module-import time, before vllm's
`triton_utils` import).

**Classic case: triton.experimental.gluon**
vllm main's `vllm/triton_utils/__init__.py` does `from triton.experimental
import gluon`. triton-ascend 3.2.1's gluon module references `constexpr_type`
which doesn't exist in its `triton.language.core` -> ImportError on every
vllm import -> all E2E tests fail.

**Fix** (see PR #13137): add to `vllm_ascend/__init__.py` at module level:
```python
import importlib.util
import os
import sys
from types import ModuleType

_triton_available = importlib.util.find_spec("triton") is not None

if os.getenv("VLLM_VERSION", "") != "0.26.0":  # skip if release already has gluon
    for _stub in ("triton.experimental.gluon", "triton.experimental.gluon.language"):
        if _stub not in sys.modules:
            sys.modules[_stub] = ModuleType(_stub)
    if _triton_available:
        try:
            import triton.language.core as _tl_core  # type: ignore[import-untyped]
        except Exception:
            pass
        else:
            if not hasattr(_tl_core, "_aggregate"):  # vllm main post-0.26.0
                _tl_core._aggregate = lambda *a, **kw: None
```

**Decision rule**: when E2E fails with `ImportError: cannot import name 'X'
from 'Y'` where Y is a third-party dep (triton, torch, etc.) and X is a
symbol vllm main newly references, do NOT mark as no-op/env-flake. Add a
compat stub in `vllm_ascend/__init__.py` (module-level, before vllm imports).
Use `vllm_version_is()` guard if the stub should only apply to certain
versions.

## triton-ascend kernel authoring constraints (NPU Triton kernels)

When syncing/porting an upstream Triton kernel that vllm-ascend
monkey-patches (e.g. `postprocess_mamba_fused_kernel` via
`patch_mamba_utils.py`), triton-ascend rejects constructs that upstream
triton accepts. Each rejected construct surfaces as a SEPARATE E2E
`EngineDeadError` / `CompilationError` round — write the kernel right the
first time by applying all four rules below:

**1. 3-term boolean chains are rejected**
- Symptom: `triton.compiler.errors.CompilationError: UnsupportedLanguageConstruct`
  on `if A and B and C:`.
- Fix: parenthesize so every level has exactly 2 terms:
  `if (A and B) and C:` — triton-ascend accepts this form.

**2. Mixed int32/int64 loop/range math is rejected**
- Symptom: `AssertionError('Mismatched type for copy_start between then
  block (int32) and else block (int64)')`.
- Fix: keep all range/tile math in ONE dtype — cast to int64 explicitly:
  `copy_size = a.to(tl.int64) * b.to(tl.int64)`,
  `tile_start = tile_idx.to(tl.int64) * per_tile`. Never let an int32
  literal leak into a branch that computes an int64 bound.

**3. DT_UINT64 is unsupported on NPU**
- Symptom: runtime error in `aclnnInplaceZero` (e.g. MambaCopyBuffers
  tensors) or kernel compile failure on `tl.pointer_type(tl.uint64)`.
- Fix: use `tl.int64` / `tl.uint8` pointers instead of `tl.uint64`.
  Upstream kernels that vectorize with u64 (e.g. `_memcpy_u64_tiled`) must
  be ported as byte-wise or int64 copies.

**4. Upstream adds a kernel constexpr param → launch `KeyError`**
- Symptom: `KeyError: 'Keyword argument <NAME> was specified but
  unrecognised'` from `triton/runtime/jit.py` at kernel launch — surfaces as
  `EngineDeadError` in E2E. Upstream changed the kernel signature (e.g.
  added `TEMPORAL_TILES` + 3D grid); the vllm-ascend patched kernel wasn't
  synced.
- Fix: sync-port the new param with a DEFAULT that preserves the old
  contract — `NAME: tl.constexpr = 1`. Triton pads 2D grids to `(g0, g1, 1)`,
  so `tl.program_id(2)` is 0 for old callers and the untiled path is
  preserved. For the mamba case, sync upstream's
  `_memcpy_u64_tiled` / `_copy_mamba_state_block` structure
  (vllm/v1/worker/mamba_utils.py, kernel def ~line 250, launch sites
  ~lines 893/991/1028) while keeping rules 1-3.

**Classic case — mamba `TEMPORAL_TILES` (run 31553496227)**: the sync missed
the new param, then two fix rounds hit rules 1 and 2 in sequence before the
3-attempt budget ran out. Full step-by-step guidance is in vllm-report
lesson L20260812-001 — in fix mode, query
`get_adaptation_lessons(keywords=["TEMPORAL_TILES"])` FIRST.

## `device_index` must be passed explicitly (not ambient)

When calling NPU device APIs (e.g. `npu_generate_uuid()`,
`torch.npu.current_device()`), always pass `device_index` explicitly from
`self.device.index` or the caller-provided index. The ambient current
device (`torch.accelerator.current_device_index()`) is NOT guaranteed to
match `self.device` - in multi-card or DP scenarios the ambient device
can be wrong, causing `ValueError` (wrong UUID) or silent corruption.

```python
# Wrong - uses ambient device, may not match self.device
device_index = self.device.index
uuid = npu_generate_uuid()  # falls back to torch.accelerator.current_device_index()

# Right - pass device_index explicitly
uuid = npu_generate_uuid(device_index)
```

This applies to ALL NPU device-specific calls inside version-guarded
branches - both the `if vllm_version_is(...)` (old) and `else` (new)
branches must pass `device_index` consistently.

## Variable name shadowing

Do not name a local variable the same as a variable in an enclosing
scope (module-level, class-level, or outer function). The inner variable
silently overrides the outer one, causing `AttributeError` or wrong-type
errors at the call site that uses the outer variable.

```python
# Wrong - local `client` shadows the global `client` (OpenAI client)
client = HTTPVLLMWeightSyncClient(base_url=BASE_URL)
# ... later ...
generate_completions(client, ...)  # uses HTTPVLLMWeightSyncClient, not OpenAI!

# Right - rename the local
sync_client = HTTPVLLMWeightSyncClient(base_url=BASE_URL)
```

When adding version-guarded code, check that new local variables don't
shadow existing names in the same file - grep for the variable name
before introducing it:
```bash
grep -n "client" <file>.py  # check for existing uses
```

## mypy error codes (final quality gate)

When the final quality gate runs mypy, failures carry error codes in `[...]`.
Fix per-code:

| Code | Meaning | Fix |
|------|---------|-----|
| `[override]` | subclass method signature incompatible with base | update subclass signature to match base (add missing params, fix types) |
| `[call-arg]` | wrong number/type of arguments at call site | fix call site; use keyword args for new params |
| `[arg-type]` | argument type mismatch | fix argument type, add cast, or guard by version |
| `[return-value]` | return type mismatch | fix return type or add cast |
| `[assignment]` | incompatible assignment | fix variable type or add cast |
| `[import-not-found]` | module not found (version-guarded import) | add `# type: ignore[import-not-found]` on the import line |
| `[import-untyped]` | module installed but missing `py.typed` (triton, torch_npu) | add `# type: ignore[import-untyped]` on the import line |
| `[attr-defined]` | attribute not found | fix attribute name, or `# type: ignore[attr-defined]` if dynamic |
| `[no-redef]` | redefinition of name | use different name or guard with version |
| `[misc]` | other | read the message, fix accordingly |
| `[valid-type]` | invalid type annotation | fix the annotation |
| `[func-returns-value]` | function declared no return but returns value | fix return annotation or remove return |

**Decision rule**:
- `[import-not-found]` / `[import-untyped]`: use `# type: ignore[<code>]`
  (the module genuinely lacks stubs or doesn't exist at this version).
- All other codes: **fix the code**.  `# type: ignore` is a last resort
  and will be flagged in review - it masks real type errors.  Only use it
  when the type system genuinely can't express the runtime behavior (e.g.
  dynamic attribute injection), and add a comment explaining why.

**Common pattern**: `[override]` on a subclass method usually means upstream
changed the base class signature - grep for ALL overrides of that method
and update every one (see §"Missing override in sibling class").

