# Main2Main Review Best Practices

> Compiled from review interactions across 55 merged main2main PRs.
> Do not skip rules based on subjective judgment.

Sections 1–8 are rationale; §9 is the operative checklist.

---

## 1. Version Compatibility

### 1.1 Version Guard Direction
`vllm_version_is()` `if` / `if not` direction must be correct. Common mistake: intending to add compatibility for a newer version but writing `if vllm_version_is("X.Y.Z")` instead of `if not`.

### 1.2 No Exact Version Matching
Never use `== "X.Y.Z"` exact patch-version matching. Use `>=` or `vllm_version_is()` range checks instead.

### 1.3 Test Skip Condition Version Numbers
`@pytest.mark.skipif` version strings must match the target upgrade version.

### 1.4 Verify Registries When Removing Old-Version Code
Before removing old-version branches, confirm `KVCacheSpecRegistry.register()` / `__init_subclass__` / module-level registrations are not deleted. Classic example: removing `AscendMambaManager` but forgetting `KVCacheSpecRegistry.register(MambaSpec, AscendMambaManager)`.

### 1.5 Never Arbitrarily Remove Env Vars or Utility Functions
Even if a function appears to be used by only one version path, grep the full call chain before deleting. Classic example: `vllm_version_is()` looks trivial but multiple patch files depend on it.

---

## 2. Function Signature Compatibility

### 2.1 New Parameters Must Not Break Positional Compatibility
When adding parameters to a patched function, either place them after `**kwargs` or guard them with a version branch. Never change the positional order of existing parameters.

### 2.2 Triton Kernel Parameters Must Match the Kernel Signature
Every argument passed to a Triton kernel call must exist in the kernel function signature. Classic example: passing `multibuffer=False` to a kernel that does not define it.

### 2.3 TorchDynamo Compilation Restrictions
Never call `logging.Logger.debug()` on a `torch.compile` code path. Guard with `if not torch.compiler.is_compiling()`.

### 2.4 Use getattr / Guards for Cross-Version Parameters
When an upstream parameter changes type across versions, use `getattr(obj, "param", default)` or a `vllm_version_is` branch.

---

## 3. Registration and Initialization Completeness

### 3.1 `__init__` Must Call `super().__init__()`
Verify every subclass `__init__` calls its parent initializer.

### 3.2 Platform Guards
Add `if not is_310p:` (or equivalent) guards around global imports or init code targeting specific platforms (310P / 910B / 910C).

### 3.3 Verify Registrations After Touching KVCacheSpecRegistry / register
After any change involving these registration points, run `find` + `grep` to confirm nothing was missed.

---

## 4. Import Verification

### 4.1 Verify Module Exists Before Adding an Import
Before writing `from vllm.X import Y`, confirm the file exists:
```bash
find ${VLLM_DIR}/vllm -name "X.py"
```
Classic example: importing `vllm.tool_parsers.deepseekv4_tool_parser` when the module does not exist in the current vllm version, causing `import-not-found` from mypy.

### 4.2 Version-Guard New Imports
If a module only exists in certain vllm versions, conditionally import with `vllm_version_is()`. After each main2main run, audit all new imports for version guards.

### 4.3 Import Path Changes
When an upstream module is renamed or moved, check whether vllm-ascend import paths need updating.

---

## 5. Error Handling and Boundary Conditions

### 5.1 `next()` Must Have a Default Value
All `next(iter, default)` calls must supply a second argument. Classic example: bare `next(...)` raises `StopIteration`.

### 5.2 Resolve Paths Before Chaining
Call `.resolve()` before chaining `Path()` operations. Classic example: `Path.parents[N]` without resolve raises `IndexError`.

### 5.3 Validate Block / Memory Calculation Boundaries
Any calculation involving `kernel_block_size` vs `logical_block_size` must handle the `kernel_block_size > logical_block_size` edge case.

---

## 6. Code Cleanup

### 6.1 Remove Dead and Commented-Out Code
Remove experimental commented-out lines (e.g. `# "FusedMoE": AscendFusedMoE,`) before submitting a PR.

### 6.2 Clean Up Redundant `type: ignore` and Stale Comments
Remove `type: ignore`, meaningless annotations, and empty-line comments when editing nearby code.

### 6.3 Document Default Value Changes
When changing a parameter's default value (e.g. `swiglu_limit: 0 → None`), explain the reason in `step_summary.md` or an inline comment.

---

## 7. Large-Scale Refactoring

### 7.1 Split Config by Version
When dealing with extensive version differences, split into separate files (e.g. `_v023.py` / `_main.py`) rather than filling a single file with if/else branches.

### 7.2 Verify Call Chains After Refactoring
After a large refactoring, grep for every moved or deleted function name to confirm no callers were missed.

---

## 8. Timing and Process

### 8.1 Do Not Sync Before a Release Is Published
main2main must not sync unreleased content from main. The target commit must be after (or at) the latest release tag.

### 8.2 Rebase Stale PRs
If a PR has not been merged for more than 2 weeks, rebase onto the latest upstream main.

---

## 9. Pre-Submit Checklist

- [ ] All `vllm_version_is()` guards are in the correct direction (if vs if not)
- [ ] No exact version matching `== "X.Y.Z"`
- [ ] Patched function signature changes keep parameter passing compatible (positional vs keyword)
- [ ] Triton kernel arguments match the kernel signature
- [ ] No `logging.debug` on any TorchDynamo compilation path
- [ ] All `super().__init__()` calls present
- [ ] All `KVCacheSpecRegistry.register()` calls accounted for
- [ ] All new imports are version-guarded
- [ ] All `next()` calls have a default value
- [ ] No dead code, commented-out code, or redundant `type: ignore` markers
- [ ] Old-version registrations migrated after deletion
- [ ] Platform-specific code has guards
