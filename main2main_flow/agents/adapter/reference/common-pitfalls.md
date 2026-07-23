# Common Pitfalls

Mistakes that break CI or cause silent failures.

## Format violations (E501 / F821 / F841 / F401)

These are all caught by pre_ci and block adaptation.  Proactively avoid them:

| Code | Meaning | Prevention |
|------|---------|------------|
| E501 | Line too long (>120) | Break lines BEFORE committing.  `== "X"` → `in (...)` expansions always exceed 120 chars. |
| F821 | Undefined name | Every `vllm_version_is()` call needs `from vllm_ascend.utils import vllm_version_is`. |
| F841 | Unused variable | Remove or prefix with `_`. |
| F401 | Unused import | Delete the import line. |

### Indentation errors

When inserting version-guard blocks into existing code, **count the leading
spaces** of surrounding lines in the same block.  Copy the count exactly.
Do not eyeball it.  Indentation errors can survive `ruff format` and
require manual fixing — they are the #1 cause of failed pre_ci attempts.

## Importing modules that don't exist (yet)

**Symptom**: mypy reports `import-not-found` for a `from vllm.X import Y` line.
This happens even when the import is guarded by `vllm_version_is()` — mypy
checks all static code paths, not just the runtime branch.

**Prevention for version-guarded imports**: Add `# type: ignore[import-not-found]`
to every import that only exists in some vllm versions:

```python
# Wrong: mypy fails on the version where this module doesn't exist
from vllm.X import Y

# Right: mypy suppresses the error on versions where the module is absent
from vllm.X import Y  # type: ignore[import-not-found]
```

**Prevention for new imports**: Before writing any unconditional `from vllm.X import Y`,
verify the module exists in the upstream tree at the target commit:

```bash
find ${VLLM_DIR}/vllm -name "X.py"
```

## Patching symbols from deleted upstream modules

**Symptom**: A patch file patches `vllm.X.Y` but `vllm.X` was deleted upstream.
The patch silently fails or raises `ModuleNotFoundError` at runtime.

**Prevention**: Check whether the upstream file still exists before patching:

```bash
test -f "${VLLM_DIR}/vllm/X.py" || echo "MODULE DELETED"
```

If deleted, remove the corresponding vllm-ascend patch and note the reason in
`step_summary.md`.

## Version guard direction is inverted

**Symptom**: New upstream-main behavior runs on the release version instead, or
old behavior runs on main.  The most common review catch (8 occurrences in merged
PRs).  This is the #1 cause of failed main2main PRs.

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

## `hasattr` / `try-except` used instead of version guard

**Symptom**: Code uses `if hasattr(obj, "new_field")` or `try: import X except:
pass` instead of `vllm_version_is()`.  These pass pre_ci but break when the
upstream type changes in the same direction.

**Prevention**: Always use `vllm_version_is("{release_tag}")` for version
boundaries.  `hasattr`/`try-except` silently mask the wrong kind of change.

## Fix mode cheat sheet

### Correlate error to upstream change

1. Extract a search term from the error (method name, config field, class name)
2. Search the upstream patch (`{patch_path}`) for that term
3. Identify the upstream intent: rename, removal, new parameter, new method
4. Map to the vllm-ascend code that depends on it
5. Decide if a `vllm_version_is` guard is needed

### Ruff lint error codes

| Code | Meaning | Fix |
|------|---------|-----|
| E501 | Line too long (>120 chars) | Break the line; use intermediate variables |
| F821 | Undefined name | Missing import — add it |

### Common typos that break codespell / typos

These words are frequently misspelled by AI code generation.  codespell and
typos run in CI and will block the PR.  Check every added comment and string
against this list:

| Wrong | Right |
|-------|-------|
| `unparseable` | `unparsable` |

> If you add a new word to a comment or docstring, ask yourself: "is this
> spelled correctly?"  A single codespell/typos violation fails the entire
> lint CI job.
| F841 | Unused variable | Remove or prefix with `_` |
| I001 | Unsorted imports | Sort manually |
| B007 | Loop variable not used | Rename to `_` |

Ruff format can auto-fix some issues but NOT E501/F821/F841 — these need
manual code edits at the exact line reported in the error.

**Fix workflow for format failures**: (1) open pre_ci_check.json → find the
format check → read every violation line → (2) for each file:line:col:CODE,
open the file at that line → (3) apply the fix from the table above → (4) run
`bash format.sh` → (5) if still fails, repeat from step 2 until clean.
This is a hard blocker — do not proceed to any other task until every format
violation is resolved.

## Leaving dead code after upstream fixes a bug

**Symptom**: vllm-ascend has a workaround for an upstream bug.  Upstream fixes
the bug, the workaround is no longer needed, but it stays in the codebase —
conditionals that always take one branch, shim functions that do nothing.

**Prevention**: When upstream fixes a bug, aggressively remove the workaround
and simplify any logic gated on it.  See the `_needs_routed_expert_parameter_aliases`
example: went from 20 lines matching 10+ model types to 1 line.

## Signature mismatch after upstream API change

**Symptom**: `TypeError: got an unexpected keyword argument 'X'` or
`missing 1 required positional argument` at call sites or overrides.

**Prevention**: Compare the upstream signature (in the patch) with every
vllm-ascend call site.  For added parameters, add a keyword default.
For removed parameters, guard with `vllm_version_is()` and pass
conditionally.  `adaptation-patterns.md` §1–§1b cover this in detail.

**Positional argument order after upstream inserts a parameter**:

When upstream inserts a new parameter between existing ones (not at the end),
positional callers in the `else` branch must match the new order.  Passing the
right number of arguments but in the wrong order is a silent bug — no
TypeError, just wrong behavior.

```python
# Upstream: get_num_blocks(..., total_computed, num_local, num_main)
# Old:      get_num_blocks(..., total_computed, num_main)

# Wrong: right count, wrong order
self.get_num_blocks_to_allocate(
    ..., total_computed_tokens, num_tokens_main_model, num_local_computed_tokens
)

# Right: matches new upstream order
self.get_num_blocks_to_allocate(
    ..., total_computed_tokens, num_local_computed_tokens, num_tokens_main_model
)
```

**Prevention**: Always use keyword arguments for new parameters in the
`else`/`not` branch.  This avoids positional-order bugs entirely.

**The output-buffer trap**: When upstream changes from `output[:] = result`
pattern to `return result`, merely redirecting a forward method (e.g.
`_GDN_PATCH_TARGET.forward = ...`) is NOT enough.  You must also:

1. Make the removed parameter optional: `output: torch.Tensor = None`
2. Guard the return path: old branch writes to `output[:]`, new branch
   does `return out`
3. Guard every call site that passes `output=...`
4. Run ALL tests — errors may only surface in specific model paths

This trap caused a real CI failure (PR #12039, step-1): the adapter
added only a forward redirect, missing the `output` parameter change.
The same upstream commit was previously adapted correctly (PR #12020)
with proper version guards, proving that a single-line fix can look
"clean" but be wrong.  `adaptation-patterns.md` §1b has the
before/after code example.

## Processor/multimodal compat patch blocked by early return

**Symptom**: After upstream removes processor registrations from
`_CLASS_TO_MODULE` (e.g. `HunYuanVLProcessor` deleted from
`processors/__init__.py`), a model test fails with
`Tokenizer is missing required attribute 'image_token'`.

**Root cause**: vllm-ascend has a compat patch (e.g.
`hunyuan_vl_processor_compat.py`) that has an `install_*` function
with an early-return guard:

```python
if not _remove_stale_registry_entries():
    return   # ← BUG: returns even when processor still needs patching!
```

Upstream already removed the stale entries, so `_remove_stale_registry_entries`
returns `False` (nothing to remove), and the entire else-branch is skipped.
The compat processor never gets installed, the tokenizer never gets its
special tokens registered, and the upstream processor construction fails.

**Fix**: Remove the `if not ...: return` guard.  The processor patch
must always run, even when the stale-registry cleanup is a no-op:

```python
_remove_stale_registry_entries()  # no-op if already clean, but we must continue
from vllm.model_executor.models import hunyuan_vision as main_hunyuan_vision
_patch_hunyuan_processor_loader(main_hunyuan_vision)
```

**Also**: When upstream adds new tokenizer attribute accesses (e.g.
`hf_processor.image_token` in `_call_hf_processor`), register the
special tokens on the tokenizer BEFORE calling
`ctx.get_hf_processor()` — Transformers may validate the tokenizer
before the processor's `__init__` runs, so putting the setup in
`__init__` is too late.  Use `getattr(self.ctx, "tokenizer", None)`
to access the tokenizer early.

This trap caused a real CI failure on PR #12070: the HunYuan-VL test
broke because the compat patch's else-branch was skipped, and the
tokenizer image tokens were never registered.

**Symptom**: `AttributeError: 'X' object has no attribute 'Y'` because
upstream moved a field to a different config object or class.

**Prevention**: Read the upstream patch to see where the field moved.
Use `vllm_version_is()` to access it from the old or new location.
Consider a small helper if the same access pattern repeats.

## Registry or plugin contract changed

**Symptom**: Backend not found, factory TypeError, or object from registry
doesn't match expected protocol.  vllm-ascend integrates through upstream
extension points (backends, platforms, model loaders).

**Prevention**: Find the changed registry key, factory signature, or
constructor in the upstream patch.  Update the vllm-ascend registration
to match.  Use a version guard only if both old and new contracts must
be supported.

## Variable aliases as base classes

**Symptom**: mypy `[valid-type]` and `[misc]` errors on class definitions
like `class X(_Base):` where `_Base = SomeClass` is a simple variable
assignment.  CI fails even though the code runs correctly.

**Root cause**: When upstream merges two classes (e.g. `PrefillA` + `DecodeA`
→ `Manager`), the adapter creates `_PrefillBase = Manager` and uses it as a
base class.  mypy treats `_PrefillBase` as a variable, not a type, and rejects
it as a base class.  `# type: ignore[name-defined]` does NOT suppress
`valid-type` or `misc`.

**Prevention**: Use the imported class directly as the base class, or use
`TypeAlias`:

```python
# Wrong: variable alias, mypy rejects as base class
_PrefillManagerBase = SpeculatorCudaGraphManager
class PrefillEagleAclGraphManager(_PrefillManagerBase):  # [valid-type] [misc]
    ...

# Right option 1: use the class directly
class PrefillEagleAclGraphManager(SpeculatorCudaGraphManager):
    ...

# Right option 2: use TypeAlias
from typing import TypeAlias
_PrefillManagerBase: TypeAlias = SpeculatorCudaGraphManager
class PrefillEagleAclGraphManager(_PrefillManagerBase):  # mypy happy
    ...
```

**ALSO**: If the imported class (`SpeculatorCudaGraphManager`) was added only
on main, the import MUST be version-guarded.  An unconditional import of a
class that doesn't exist on the release version will `ImportError` at runtime.
See SKILL.md checklist item 4.

## effective_block_size vs physical block_size for hit_length

**Symptom**: v0.25.1-only native crash (segfault, no Python traceback) during
or after `llm.generate()` on MLA models (DeepSeek-V2-Lite/V3/V4).  Main passes.
The crash is often intermittent - ENPU mode may mask it.

**Root cause**: When upstream changes a function's return type so that v0.25.1
must compute `hit_length` externally (e.g. `find_longest_cache_hit` returns
just `blocks` on 0.25.1 but `(blocks, hit_length)` on main), the external
computation must use the **physical** block size, not `_get_effective_block_size()`.

`_get_effective_block_size()` multiplies by `compress_ratio` for MLA specs.
But `hit_blocks[0]` holds **physical** blocks (each `spec.block_size` tokens),
not logical blocks.  Multiplying physical block count by logical block size
inflates `hit_length` by `compress_ratio` (e.g. 4x for DeepSeek MLA).

```python
# Wrong: effective_block_size includes compress_ratio -> 4x over-count
if vllm_version_is("{release_tag}"):
    hit_blocks = hit_result
    _new_hit_length = len(hit_blocks[0]) * effective_block_size  # 4 * 512 = 2048

# Right: use physical block_size
if vllm_version_is("{release_tag}"):
    hit_blocks = hit_result
    block_size = spec.block_size
    if self.dcp_world_size * self.pcp_world_size > 1:
        block_size *= self.dcp_world_size * self.pcp_world_size
    _new_hit_length = len(hit_blocks[0]) * block_size  # 4 * 128 = 512
```

The inflated `hit_length` tells the scheduler that more tokens are cached than
actually exist.  The model skips computing those "cached" tokens and accesses
out-of-bounds KV cache entries -> segfault.

**Why main is unaffected**: on main, `find_longest_cache_hit` returns
`(blocks, hit_length)` where `hit_length` is computed inside the manager with
the correct logical-block semantics.  The external recomputation only happens
on the release-tag branch.

**Check ALL sibling functions**: this bug pattern was found in
`find_longest_cache_hit_per_group` (fixed) but the identical bug in
`find_longest_cache_hit` (regular scheduling path, called during every
`llm.generate`) was missed for a full CI cycle.  When fixing a version-branch
computation bug, grep for every call site that recomputes the same value:

```bash
grep -n 'effective_block_size\|_get_effective_block_size' vllm_ascend/patch/platform/patch_kv_cache_coordinator.py
```

## Same bug in multiple code paths - fix them all at once

**Symptom**: You fix a version-branch bug reported by one failing test.  The
next CI run reveals a *different* test failing from the *same* root cause in a
*sibling* function you didn't check.

**Prevention**: When a bug is caused by a version-branch returning a different
type or computing a value externally, the same bug almost certainly exists in
every sibling function that handles the same upstream return type.  Fix them
all in the same commit.

Real example from PR #12502 / #12648:

| function | called by | first fix | missed for 1 cycle |
|----------|-----------|-----------|--------------------|
| `find_longest_cache_hit_per_group` | recompute scheduler only | yes | - |
| `find_longest_cache_hit` | **regular `llm.generate()` scheduling** | no | yes |

The per_group function was fixed because a unit test caught it.  The regular
function was missed because no unit test exercised it with compressed MLA on
v0.25.1 - only a 4-card e2e test (`test_aclgraph[CASE_DS_ACLGRAPH]`) caught
it, and that test crashed with no traceback, making diagnosis harder.

**Action**: after fixing any version-branch computation bug, immediately:

1. Grep for the same pattern (`effective_block_size`, `len(hit_blocks[0])`,
   etc.) in the same file and sibling files
2. Fix every occurrence in the same commit
3. Note in `step_summary.md` that sibling functions were audited

## v0.25.1-only segfault after llm.generate -> suspect hit_length

**Symptom**: e2e test crashes on v0.25.1 with `AssertionError: Expected N
results, got 0` and `Error: Timeout waiting for worker results. A worker
might have crashed.`  No Python traceback.  Main passes the same test.

**Diagnosis flow**:

1. Check if the test uses an MLA model (DeepSeek-V2-Lite/V3/V4, Qwen3-MoE
   with MLA).  MLA specs have `compress_ratio > 1`.
2. If yes, grep for `effective_block_size` and `_get_effective_block_size` in
   `vllm_ascend/patch/platform/patch_kv_cache_coordinator.py` and
   `vllm_ascend/core/single_type_kv_cache_manager.py`.
3. Any line that multiplies `len(hit_blocks[0])` (physical block count) by
   `effective_block_size` (logical block size) on the release-tag branch is
   the crash site.
4. The crash happens AFTER inference because the wrong `hit_length` causes the
   scheduler to mark non-cached tokens as cached.  The model reads garbage KV
   cache entries, and the segfault triggers during attention backward or
   cleanup - not at the point of the wrong computation.

**Why ENPU may mask it**: ENPU uses different operator implementations with
different memory layouts.  The out-of-bounds access may land on valid memory
by coincidence, so the test passes with wrong results instead of crashing.
Do NOT assume ENPU-pass means the bug is absent.
