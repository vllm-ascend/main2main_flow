---
name: adapter
description: Adapt vllm-ascend to upstream vLLM changes ({mode} mode).
---
# adapter — mode: {mode}

## Efficiency Rules — avoid dead-end exploration

Each tool call (bash/grep/read) costs 10-30s of model time; a full step
should take ~10 min. Past that, you are over-exploring:

1. **Max 5 grep/glob calls per step.** If still unsure, re-read the MCP
   `get_adaptation_guide` result or the upstream patch — the answer is there.
2. **Conclude once, act immediately.** When analysis reaches a conclusion,
   implement it RIGHT AWAY. Do NOT re-state it or grep to "confirm".
3. **Read files whole, once** (`read`/`cat`); do NOT probe small `sed` ranges.
4. **Batch related checks into one bash call** (`grep X f; grep Y f; ls`).
5. **Verify at edit time**: keep lines ≤120 chars AS YOU WRITE; never run
   linters (banned — see Rules); the push-time gate feeds exact violations back.
6. **MCP context is the map — but only when it covers the commit.** If the
   guide returned data, follow it; grep only CALL SITES and sibling overrides
   (checklist 9-10). If empty, the guide is useless — analyze the diff yourself.
7. **One pass per file** — edit all spots, verify once, move on.
8. **Fix mode: fix ONLY what the error says** — the exact line the traceback
   or violation names.


## Repositories

| repo | path |
|------|------|
| vllm (read-only) | {vllm_path} |
| vllm-ascend (edit here) | {ascend_path} |

## Inputs

| field | value |
|-------|-------|
| mode | {mode} |
| step | {step_id} |
| last step | {is_last_step} |
| release tag | {release_tag} |
| upstream patch | {patch_path} |
| changed files | {changed_files_path} |
| archive dir | {step_dir} |

## Error Content (inlined)

{error_content}

## Rules

- Only modify vllm-ascend at {ascend_path} (never vLLM at {vllm_path})
- Do NOT run git add/commit/reset/checkout in vllm-ascend
- Use `vllm_version_is("{release_tag}")` for version boundaries — never `hasattr`/`try/except`; all guard branches must have identical function signatures
- **Guard new upstream imports too**: a module-level `from vllm... import NewSymbol` crashes old fixed branches (v0.27.1) at import time. Import inside the `vllm_version_is` branch or lazily at the use site, then grep the file to VERIFY no unguarded import remains (PR #14517: guarded call site, unguarded module-level `BatchReqState` import broke the v0.27.1 lane)
- Static analysis only — never import vllm/vllm-ascend, run tests, launch models, or require NPU/GPU
- Use `rg` for symbol search; batch related lookups into one invocation
- Reference docs are **index-first**: `## Index` tables map sections to line ranges. Read ONLY the index + sections whose trigger matches (`sed -n 'A,Bp' <file>`); never read a whole reference file; never re-read content already in session
- **NEVER run mypy, ruff, pre-commit, py_compile, or any linter/compiler.** Only read code and edit files.
- Never read raw CI logs — use inlined error content above
- Do NOT treat ModuleNotFoundError or missing NPU/GPU from local commands as adaptation failures
- **NEVER modify tests/e2e/ — E2E cases are frozen** (edits are reverted, attempt voided); adapt `vllm_ascend/` source instead. `tests/ut/` MAY be adapted per the UT rules below.

## UT adaptation rules (write tests that survive the shared-process batch)

Adapting an upstream contract change usually requires rewriting `tests/ut`
files — and the rewrites are where adaptations most often break CI:

1. **Bare-object tests** — `AttributeError: '<X> object has no attribute '<Y>'`
   on a `__new__`-constructed object (tests skip `__init__`). Fix: add the
   attribute to the test object; for OPTIONAL attributes prefer defensive
   access in source (`getattr(obj, name, default)`).
2. **Mock-contract drift** — `TypeError: unexpected keyword argument` or a
   mock wrapping an AttributeError. Fix: grep the upstream signature and sync
   the mock — do NOT run the test to discover it.
3. **Version-guarded symbol resolution** — stub surfaced as KeyError/
   AttributeError at first real use (`next()`-style AST walks grab the stub).
   Fix: grep ALL `def <name>(` and resolve the ACTUAL implementation.
4. **Adapt source and tests together** — update test mocks in the same pass;
   verify statically (grep the attribute-access chain), never by running.

## Accumulated Step Model

The working tree already contains all successful prior-step adaptations:
1. Read {previous_step_summary_path} if it exists
2. Reuse prior guards, helpers, imports, and patterns
3. Never revert prior adaptations unless the current change proves them obsolete

The step_target.patch is accumulated (git diff HEAD).

## Code Exploration

- Start from the upstream patch + changed-file list. **Key question**: does
  vllm-ascend subclass, override, call, import, or read anything this patch
  changed? Internal upstream changes need adaptation only when vllm-ascend
  directly depends on the behavior.
- **No-op claims need evidence — "forward is overridden" is NOT evidence.**
  Wrapped upstream modules (shared_experts, token_dispatcher, routed_experts,
  moe_runner) still execute their async/stream/event logic on the Ascend
  wrapper's behalf. Query `get_adaptation_lessons` for the shared-expert and
  MoE-gate contract cases (L20260819-001/002), and read
  `{step_dir}/upstream-fix-context.diff` (when in the error logs) before
  declaring no-op.
- **vllm-report impact map**: {vllm_report_context} — call the MCP tools
  DYNAMICALLY (see "vllm-report MCP Tools"); grep only for gaps MCP left.
- Use the Key Areas in code-structure-guide.md as an architecture-level
  supplement when vllm-report is unavailable; prefer codegraph tools if
  available, else grep/glob/read.
- Read enough vllm-ascend code to understand the subsystem — subclass
  chains, registration patterns, imports. Skimming beats a wrong assumption.
- **Processor/multimodal trap**: if `changed_files_path` contains
  `vllm/transformers_utils/processors/__init__.py` or `vllm/multimodal/`,
  this is NEVER a no-op. Search for `*processor*compat*.py` and verify every
  compat patch still works. See `reference/adaptation-patterns.md` §12.
- When a method signature changes, grep for ALL `def <method_name>(` in the
  vllm-ascend tree — every override must be updated.

## Workflow

### adapt mode

1. Read the upstream patch + changed file list from `{patch_path}` / `{changed_files_path}`
2. Read the vllm-report impact map FIRST, then targeted search to fill gaps
3. Apply minimal changes — do not refactor unrelated code
4. **Apply the Format + mypy prevention rules below WHILE editing** — they are
   NOT checked per-step (one push-time run; exact `file:LINE:CODE` comes back
   via `quality_gate.json` in fix mode). NEVER run mypy/ruff/py_compile.
   - **Ignore env noise in format output** (gitleaks "not executable",
     shellcheck missing, "Exec format error") — do NOT touch
     `.github/workflows/scripts/` to fix these.


**Guard decision tree**:

```
Does this code path need to support BOTH the release version AND upstream main?
  ├─ YES, and the API differs → use vllm_version_is("{release_tag}")
  └─ NO  → no new guard needed
```

- New parameters with defaults: no guard needed
- Constructor/factory signature changes: guard with `vllm_version_is()`
- Import path changes: guard the import, import unconditionally if the symbol
  exists in both versions

**BEFORE marking the adaptation complete, verify ALL of these:**

1. `vllm_version_is` guards: NEW code in `else`/`not` branch, OLD release code in `if`.
2. Every guarded `from vllm.X import Y` has `# type: ignore[import-not-found]`.
3. Imports that don't exist on OLD vllm: import MUST be inside `else` (not `# type: ignore`-guarded).
4. No circular imports.
5. Call sites pass correct number/type/ORDER on BOTH branches; keyword args for new params.
6. Override methods match the upstream signature.
7. **Base-class attribute sync**: upstream adds a base-class attribute → EVERY
   vllm-ascend subclass must accept and set it (base code reads `self.X` at
   runtime; "GPU-only" is NOT a reason to skip). See
   `reference/adaptation-patterns.md` §9.
8. No variable aliases as base classes — use `TypeAlias` or the class name.
9. Fixing a version-branch bug → grep the same pattern in ALL sibling functions.
10. Method signature changed → grep ALL `def <name>(` — every override updated.
11. Every `next(gen, default)` has a default — no bare `next(...)`.
12. `super().__init__()` in every subclass `__init__`.
13. No exact version matching (`== "X.Y.Z"`).
14. No dead code, commented-out blocks, or stale `# type: ignore` left behind.
15. Remaining items (registries, Triton params, getattr, path resolution):
    see `reference/common-pitfalls.md` §"Additional QA-level checks".
16. **Return type change → grep EVERY `return` in the method** (one leftover
    old-type return slips past pre_ci/mypy). See
    `reference/common-pitfalls.md` §"Return type mismatch across version
    branches".
17. **Conditional method definition**: `else` branch (new signature) MUST carry
    `# type: ignore[misc]`. See `reference/adaptation-patterns.md` §13.
18. **Triton kernel signature match**: patched kernel MUST match the upstream
    call site exactly (validated at launch). See
    `reference/adaptation-patterns.md` §14.
19. **`device_index` passed explicitly** — from `self.device.index`, not the
    ambient current device. See `reference/common-pitfalls.md`
    §"`device_index` must be passed explicitly (not ambient)".
20. **No variable shadowing**: grep the file for a name before introducing it.

**Format rules — apply WHILE editing, not after:**

- Every line **must** be ≤ 120 characters.
- No unused imports (F401), unused variables (F841), undefined names (F821).
- Every `vllm_version_is()` call needs `from vllm_ascend.utils import vllm_version_is`.
- Imports sorted: stdlib → third-party → first-party.

**Output-buffer trap**: When upstream changes from `output[:] = result` to
`return result`, don't just redirect a forward method. You MUST make the
removed parameter optional, guard the return path, and guard every call site.
See `reference/adaptation-patterns.md` §1b.

### fix mode

The tree already contains the failed adaptation — do NOT start from scratch.
Fix ONLY what the error says (Efficiency rule 8).

**Pre-CI failures**: open `pre_ci_check.json` → `violations` carry exact
file:line:col:CODE. Fix those specific lines.

**E2E test failures**: open `round-N-result.json` → if `code_bugs_count` > 0,
read the failed tests' `-summary.json` (code_bugs/env_flakes) + `.log`:
1. Read the FULL traceback — identify the exact failing path (normal vs
   cache, with-data vs no-data, batch vs single). Do NOT guess.
2. **MUST call `get_adaptation_lessons(keywords=["<error message / test
   name>"])` BEFORE fixing** and follow its fix_guidance (MCP failure → log,
   continue). Skipping may repeat a documented mistake.
3. Check `reference/common-pitfalls.md` for a KNOWN failure with this exact
   message; follow its fix requirements if one matches.
4. **Multi-path check (#1 reason fixes fail)**: is the patched function CALLED
   on the failing path? Does the fix cover ALL paths to the asserted
   invariant? See §"Fix covers only ONE of multiple code paths".

**ImportError is NOT an env flake** — a real adaptation gap. For
`ImportError: cannot import name 'X'` from a pinned dep where X is newly
referenced by vllm main, add a compat stub in `vllm_ascend/__init__.py`
(module-level, before vllm imports) — see
`reference/common-pitfalls.md` §"Environment compatibility stubs"
(triton.experimental.gluon case, PR #13137). Do NOT mark no-op/env-flake.
Stub imports of untyped modules (e.g. triton) need
`# type: ignore[import-untyped]`.

**Final quality gate failures (push-time format + mypy)**: after all steps,
format + mypy run once on the accumulated diff; `error_logs` then contains
`quality_gate.json` (NOT `pre_ci_check.json`):
```json
{{"all_passed": false, "checks": [
  {{"name": "format", "violations": ["file.py:LINE:CODE ..."]}},
  {{"name": "mypy",  "violations": ["file.py:LINE:COL: error: ... [override]"]}}
]}}
```
Fix format by `file:LINE:CODE` (E501 break line, F401 delete import); mypy
per error code — see `reference/common-pitfalls.md` §"mypy error codes".
Mechanical — do NOT re-analyze the upstream patch. If e2e re-runs and
fails, the fix regressed — revert and retry.

## Output

Write to {step_dir}/:

| file | content |
|------|---------|
| analysis.md | subsystems touched, changes, version guard assessment |
| step_summary.md | accumulated summary (preserve prior, append `{step_id}` section) |
| result.json | `{{"status": "adapted" \| "noop", "files_touched": [...]}}` — write LAST |

### step_summary.md

No-op — ONE line: `- {step_id}: No-op — <reason>`

Adapted:
```
- {step_id}: Adapted — <files>
  Upstream source: [<sha>](https://github.com/vllm-project/vllm/commit/<sha>)
  Cause: <what changed upstream — 1-2 sentences on the upstream diff>
  Change: <what was done in vllm-ascend — specific files, guards, new params>
```

**Cause vs Change must be DIFFERENT** (Cause = upstream diff, Change =
vllm-ascend's adaptation). Multi-line fields: indent continuations 2 spaces.

## Last Step Only

If {is_last_step}: check code-structure-guide.md freshness. If stale, write
the updated version as {step_dir}/{code_structure_guide_file}.

## vllm-report MCP Tools (on-demand)

A vllm-report MCP server is registered — **read-only** tools for deeper info
beyond the injected impact map.

### How to use MCP tools - decision flow

```
1. START HERE — call MCP BEFORE grepping:
   ├─ get_adaptation_guide(sha=<end_commit>) FIRST — data → follow it
   │  (Efficiency 6); empty → analyze the diff yourself.
   ├─ get_cross_project_mapping() — vllm path → ascend file map.
   └─ Skip grep only when MCP data exists; otherwise grep is your analysis.
2. HOW to adapt? → get_adaptation_guide / get_patch_catalog(category=...) /
   search_analysis(...).
3. Understand a subsystem? → get_key_abstractions(repo="vllm-ascend") /
   get_module_info(repo, module_name) / get_development_workflows().
4. Fix mode: why did a test fail? → search_analysis(keywords=[...]) /
   get_commit_arch_delta(repo="vllm", sha=<end_commit>).
```

**CRITICAL**: MCP FIRST, before any grep — grepping is FALLBACK for files MCP
didn't mention. Typical savings: 31 greps → 3-4 MCP calls + 2-3 greps.

### Tool quick reference

| Tool | Key parameters | What it returns |
|------|---------------|-----------------|
| `get_adaptation_guide` | `sha` | Step-by-step guide with line numbers |
| `get_cross_project_mapping` | (none) | patch_impact_map + vllm_to_ascend_map + impact_judgment_rules |
| `get_interface_surface` | `repo` | 8 inheritable interfaces with ascend_impl + key_methods |
| `get_patch_catalog` | `category` (platform\|worker, optional) | Patches with targets/why/how/related_pr |
| `search_analysis` | `keywords[]`, `tags[]`, `date_from`, `date_to` | Matching commits with ascend_impact |
| `get_key_abstractions` | `repo` | Core abstractions with inheritance info |
| `get_module_info` | `repo`, `module_name` | Module details (files, classes, deps) |
| `get_commit_arch_delta` | `repo`, `sha` | Affected modules + change summary |
| `get_ascend_impact_summary` | (auto from baseline) | Per-commit ascend impact for pending commits |
| `get_development_workflows` | (none) | How to add patches/models/attention backends |
| `get_pending_adaptations` | (none) | Commits pending adaptation (status, tags, impact) |

### Rules

- MCP tools are SUPPLEMENTARY to `{vllm_report_context}` — call only for
  deeper info; limit to 2-3 calls per step.
- Do NOT call `update_adaptation_status` / `advance_baseline` (write tools;
  main2main is read-only).
- Tool failure/timeout → fall back to grep/file reads immediately.
- "No ascend impact" from vllm-report but grep finds a base-class change →
  trust grep (vllm-report may not have analyzed this commit).

## Reference (read on demand)

The reference docs below are NOT inlined — carrying them in the prompt makes
every tool-call generation slower. Read a file ONLY when the current question
depends on it, and read only the relevant section (grep the file for the
heading first, then read that range).

{reference_content}