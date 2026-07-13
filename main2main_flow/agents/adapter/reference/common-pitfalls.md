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
conditionally.  `adaptation-patterns.md` §1–§2 cover this in detail.

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
