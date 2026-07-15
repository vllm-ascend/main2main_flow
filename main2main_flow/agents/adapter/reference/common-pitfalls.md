# Common Pitfalls

Mistakes that break CI or cause silent failures.

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
PRs).

**Prevention**: Ask yourself: "which branch runs the NEW code?"  The answer must
be `not vllm_version_is("...")`.  If you're adding code for upstream main, put
it in the `else` block.

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

## Config/attribute moved between classes

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
