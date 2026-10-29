# Adaptation Patterns

> **CRITICAL**: Every `vllm_version_is` guard in this document shows NEW
> upstream-main code in the `else`/`not` branch and OLD release code in the
> `if` branch. If you write a guard the other way around, it is **wrong**.

## Index

Read only the section whose trigger matches your change — never the whole file.
Lines are relative to this file; read a section with `sed -n 'A,Bp' <this file>`.

| Section | Trigger — read when... | Lines |
|---------|------------------------|-------|
| §0 Decision order: guard first | ANY compatibility change — read this before choosing a shape | 34-62 |
| §1 Upstream adds a parameter | a new param appears in an upstream signature | 64-74 |
| §1b Removes a parameter + return change | a param is removed AND return semantics change | 76-91 |
| §2 Constructor/factory signature change | a constructor or factory changes signature | 93-103 |
| §3 Class moved to another module | a class moves modules upstream | 105-120 |
| §4 Upstream deletes a patched module | vllm-ascend patches a module upstream deleted | 122-124 |
| §5 Upstream fixes a bug with a workaround | upstream bugfix supersedes an ascend workaround | 126-134 |
| §6 Refactoring too large for guards | >50 lines of guarded code | 136-145 |
| §7 Runtime check Ascend can't satisfy | upstream adds an NPU-unsatisfiable check | 147-153 |
| §8 PyTorch API not on NPU | upstream uses a torch API missing on NPU | 155-161 |
| §9 Base class adds attr/method | base class gains fields/methods (incl. NVIDIA-only trap) | 163-202 |
| §10 Method signature change — ALL overrides | a method signature changes; grep every override | 204-213 |
| §11 Processor registrations removed | upstream unregisters processors | 215-222 |
| §12 `next()` calls | changed code contains bare `next(...)` | 224-234 |
| §13 Params added to an overridden method | overridden method gains params (two-version def) | 236-272 |
| §14 Triton kernel signature change | a patched Triton kernel changes signature | 274-303 |
| §15 Rejected shapes (PR #20156, #20223) | about to add a helper/probe/state/slice, an env var, or a patch module | 305-424 |
| §16 Positive reference (PR #16527) | calibrating what a maintainer accepts | 426-443 |
| Mypy prevention | final mypy errors on guards/imports/overrides | 445-451 |

## 0. Decision order: guard first

**Rule**: main2main always supports two trees at once. Decide the SHAPE
before writing code — in this order:

0. Is the problem **not a code contract at all** — a wheel/op missing from
   THIS container, a driver or image quirk, a kernel selector? → fix the
   environment (upgrade the dependency) or report it. Do NOT add a
   `VLLM_ASCEND_*` environment variable (§15.6) and do NOT add a new
   module under `vllm_ascend/patch/**` (§15.7).
1. Is the difference a **contract** (signature, arity, behavior, module
   path)? → **version-guard it at the narrowest site**:
   release behavior in the `if vllm_version_is("<tag>")` branch (copied
   from the release tree, not reconstructed), upstream-main behavior in
   the `else` branch.
2. Is it a **new parameter with a default**? → add it to the signature.
   No guard (§1).
3. Is it an **overridden method gaining parameters**? → two `def`s under
   `vllm_version_is` (§13).
4. Does the **upstream base class read a new attribute** at runtime? →
   accept and set it in every vllm-ascend subclass (§9).
5. Is the whole module/class replaced (>~50 lines)? → split the file by
   version (§6).

Everything else — a new helper, a new method, a capability probe, a
state dict, a `[:N]` slice, a new env var, a new patch module — is
**rejected**; see §15. The guard is the default, and "both trees behave the
same" is a claim that must be checked against the release tree, never
assumed.

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

## 15. Rejected shapes — PR #20156 / #20223 (auto-generated, maintainer-rejected)

§15.1-15.5 were produced by an automated main2main run on a 7-file step;
§15.6-15.7 come from the same pipeline a few runs later. Each is
"functionally green, structurally wrong": it makes the release lane
inherit main's new behavior, hides the difference behind an abstraction, or
adds permanent surface for a problem that is not an upstream contract at
all. They are BLOCKING in adapter-qa and in the push-time maintainability
gate.

### 15.1 New method + new state instead of a guarded branch

`vllm_ascend/ec_manager/score_ec_manager.py` grew
`self.request_hash_refs` and a new `_add_cached_input_id()` method, and
`free_encoder_input` was rewritten so BOTH trees implement upstream main's
refcounting ("own it so both trees behave alike").

Rejected. The release tree has no refcounted release fix; mirroring it
changes release behavior and adds public surface vllm-ascend must maintain
forever. Sanctioned shape:

```python
def free_encoder_input(self, request: Request, input_id: int) -> None:
    req_id = request.request_id
    mm_hash = request.mm_features[input_id].identifier
    if vllm_version_is("0.30.0"):
        # release: drop on first free (pre-refcount behavior)
        cached_ids = self.request_cached_ids.get(req_id)
        ...
    else:
        # main: upstream's refcounted release
        cached_ids = self.request_cached_ids.get(req_id)
        ...
```

One existing method, two branches, no new attribute, no new method.

### 15.2 Capability probe standing in for a version boundary

`vllm_ascend/models/minimax_m3/msa_m3.py` added
`_cann_index_score_available()` plus a module-level
`_CANN_INDEX_SCORE_AVAILABLE` flag, then ANDed it into the two existing
`_USE_ASCENDC_INDEX_SCORE_*` flags — and five UT blocks grew a second
branch on the new flag.

Rejected: the contract difference is the version, not the installed wheel.
Probe-based flags also flip behavior inside a single lane at runtime, which
no version guard can express and no reviewer can predict.

### 15.3 Silent arity swallowing

`vllm_ascend/worker/v2/model_runner.py`:

```python
draft_split = adaptive_verification_manager.reallocate_drafts(req_ids, idx_mapping)
cu_num_logits, query_start_loc, total_num_draft_tokens = draft_split[:3]
```

Rejected: main returns a 4th element (batch-sharded sampling). Slicing makes
both trees compile and silently drops the main-only field, hiding the
contract from mypy and from the release lane. Sanctioned shape: two explicit
unpacks under `vllm_version_is`.

### 15.4 Unconditional new-upstream import / attribute

`vllm_ascend/patch/worker/patch_distributed.py` added
`from vllm.distributed.utils import get_cpu_distributed_timeout_or_none` at
module level plus `self.cpu_timeout = ...` unconditionally, in a patched
class that reimplements `__init__` without calling `super()`.

Rejected: if the symbol is main-only the release lane dies at import time.
Guard the import (§3) and set the attribute only in the branch that needs it
— or, when both trees really have it, prove it by reading the release tree.

### 15.5 Editing the repository's own tooling config

`.pre-commit-config.yaml` gained
`args: ["-shellcheck=", "-pyflakes="]` on `actionlint`, disabling two lint
subprocesses because they hang in the flow's container.

Rejected: the flow's environment problem must not be shipped as an upstream
config change; the flow suppresses hooks itself (`SKIP`), and edits to
`.github/**` / `.pre-commit-config.yaml` are reverted automatically.

### 15.6 A new environment variable as a version / kernel switch

`vllm_ascend/envs.py` gained
`"VLLM_ASCEND_INDEX_SCORE_IMPL"` (`auto`/`ascendc`/`triton`) and
`vllm_ascend/models/minimax_m3/msa_m3.py` read it to pick a kernel at import
time, because one container's CANN build did not export
`cann_ops_transformer.ops.msa_index_score`.

Rejected: `envs.py` is configuration surface, not adaptation surface. The
trigger was a missing wheel in THIS container, and the correct fix is the
dependency (upstream added `[CI] Install CANN 9.3.0 ops-transformer
packages` for exactly this reason), not a user-visible switch that silently
changes the kernel on old images. `auto` is also just the rejected
capability probe (§15.2) with an env wrapper around it.

Accepted: a `vllm_version_is` branch when the two trees really differ; a
reported environment failure when they do not. If a switch is genuinely
unavoidable, it needs `# main2main-justification: <concrete reason>` on the
line above the key, and the reason must name the upstream contract or
container fact.

### 15.7 A new patch module instead of one more guarded branch

A version difference inside a patched upstream module was handled by adding
`vllm_ascend/patch/worker/patch_v2/patch_x.py` plus a registration entry in
that directory's `__init__.py`.

Rejected: every patch module is permanent surface — it must be re-validated
against each upstream bump, it changes import order, and reviewers cannot
see which upstream contract it shadows. The existing module already owns
that upstream symbol; put the difference in a guarded branch there.

Accepted: extending the existing module (`(§1b)`/`(§13)` shapes), and
DELETING a patch when upstream makes it unnecessary. When upstream deletes
or renames the patched symbol, merge or remove the patch — a `_v2`/`_compat`
file only moves the maintenance cost.

## 16. Positive reference — PR #16527 (main2main v0.30.0)

The accepted shape for a large main2main upgrade:

- Every contract difference is a guarded branch or a defaulted parameter —
  no parallel implementation of an upstream method.
- When the pinned release tag advances, guards that became obsolete are
  **deleted** (`if vllm_version_is("0.29.0"): ... else: ...` collapses to
  the single remaining path) instead of being stacked.
- Base-class attributes that upstream reads are accepted and set; the
  feature behind them is NOT reimplemented for NPU.
- Tests lose their version forks when the fork stops being meaningful —
  the test suite tracks the source, not the capability matrix.
- Docs/Dockerfiles/commit-pin files carry the tag bump; no tooling config
  is "fixed" to make a check pass.

Use it as the calibration for "would a vllm-ascend maintainer accept this
diff as a compatibility change?".

## Mypy prevention

- **Guarded imports**: every `from vllm.X import Y` inside a version guard
  needs `# type: ignore[import-not-found]`. See §3.
- **Call-site args**: match the new upstream signature. See §1, §1b.
- **Override signatures**: all branches must have the same public signature.
  See §1b.
