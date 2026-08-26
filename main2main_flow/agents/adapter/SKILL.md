---
name: adapter
description: Adapt vllm-ascend to upstream vLLM changes ({mode} mode).
---
# adapter — mode: {mode}

## Efficiency Rules — avoid dead-end exploration

Each tool call (bash/grep/read) costs 10-30s of model time, and each
"let me think about this" gap costs 1-2 min. A full step should take
~10 min of work. If you're past that, you are over-exploring. Follow
these rules to stay on the critical path:

1. **Grep budget: max 5 grep/glob calls per step.** MCP context + 2-3
   targeted greps covers almost every step. If you've grepped 5 times and
   are still unsure, STOP grepping — re-read the MCP `get_adaptation_guide`
   result or the upstream patch instead. The answer is usually there.
2. **Conclude once, act immediately.** When your analysis reaches a
   conclusion (e.g. "this is the processor/multimodal trap, the fix is
   X"), implement X RIGHT AWAY. Do NOT re-state the same conclusion in
   follow-up text, do NOT grep to "confirm" what you already derived.
   Each repetition burns 1-2 min with no new information.
3. **Read files whole, once.** Use `read` or `cat` to load a file ONCE.
   Do NOT probe small sections with `sed -n '40,75p'` repeatedly — one
   full read of a 200-line file is faster than 4 partial reads, and the
   model sees the full context in one shot.
4. **Batch related checks into one bash command.** Combine multiple greps
   in a single call: `grep -n "X" file; grep -n "Y" file; ls dir`. Each
   bash call has fixed overhead (issue + output processing); 3 greps in
   one command is ~1/3 the cost of 3 separate calls.
5. **Verify at edit time, not in a verification loop.** Keep every line
   ≤120 chars AS YOU WRITE IT; never run py_compile/mypy/ruff (banned —
   see Rules). The push-time gate re-runs format + mypy once and feeds
   exact violations back — don't prove there are none, just don't leave
   obvious ones.
6. **MCP context is the map — don't re-discover it — BUT only when it
   covers the commit.** If `get_adaptation_guide(sha)` returned a guide,
   follow it directly — grep only for CALL SITES and sibling overrides
   (checklist 9-10). If it returned empty ("commit not covered"), the
   guide is useless — analyze the upstream diff yourself (grep + read +
   reason). The MCP gap is the signal to explore.
7. **One pass per file.** Read a file, understand it, edit all needed
   spots, verify once, move on. Do NOT return to an already-edited file
   unless a later discovery proves your edit wrong.
8. **In fix mode, fix ONLY what the error says.** Fix the exact line the
   traceback / violation names — the adaptation is done, you are fixing
   one failure.


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
- Do NOT run git add, git commit, git reset, or git checkout in vllm-ascend
- Use `vllm_version_is("{release_tag}")` for version boundaries — never `hasattr` or `try/except`
- All branches of a version guard must have identical function signatures
- **Imports of new upstream symbols must be guarded too** — a module-level
  `from vllm... import NewSymbol` crashes older fixed branches (v0.27.1) at
  import time even when every call site is guarded.  Import inside the
  `vllm_version_is` branch, or lazily at the use site.  After writing a
  guard, grep the file to VERIFY no unguarded import of the new symbol
  remains (PR #14517: the call site was guarded but the module-level
  `BatchReqState` import broke the whole v0.27.1 lane — ImportError + a
  third positional arg to `init_workspace_manager`).
- Static analysis only — do not import vllm/vllm-ascend, run tests, launch models, or require NPU/GPU
- Use `rg` for symbol search (installed) — one call covers what a loop of `grep` calls would take; batch related lookups into a single invocation
- Reference docs (`adaptation-patterns.md`, `common-pitfalls.md`,
  `code-structure-guide.md`) are **index-first**: each starts with a `## Index`
  table mapping sections to line ranges. Read ONLY the index and the sections
  whose trigger matches your change — e.g. `sed -n 'A,Bp' <file>`. Never read a
  whole reference file. Content you already read in this session stays in your
  context — do not re-read it in later attempts or after resume
- **DO NOT run mypy, ruff, pre-commit, py_compile, or any linter/checker/compiler command.** Ever. During adaptation, only read code and edit files.
- Never read raw CI logs — use inlined error content above
- Do NOT treat ModuleNotFoundError or missing NPU/GPU from local commands as adaptation failures
- **NEVER modify anything under tests/e2e/ — E2E test cases (assertions, golden values, parametrizations) are frozen.** Edits there are automatically reverted and the attempt is voided; adapt the `vllm_ascend/` source instead. (`tests/ut/` MAY be adapted per the UT rules below.)

## UT adaptation rules (write tests that survive the shared-process batch)

Adapting an upstream contract change usually requires rewriting `tests/ut`
files too — and the rewritten tests are where adaptations most often break
CI. Apply these rules while writing them; each has a recognition signal and
a fix pattern (generic versions of the failures in run 31581543851, PR
#14107).

1. **Bare-object tests** — signal: `AttributeError: '<X> object has no
   attribute '<Y>'` where X is a `__new__`-constructed object (tests use
   `__new__` to skip `__init__`). Fix: add the missing attribute to the
   test object; for OPTIONAL attributes, prefer defensive access in the
   source (`getattr(obj, name, default)`) — it stays compatible with
   upstream, which always sets the attribute in `__init__`.
2. **Mock-contract drift** — signal: `TypeError: unexpected keyword
   argument`, or a mock result wrapping an AttributeError (e.g. a Future).
   Fix: sync the mock with the upstream definition by GREPPING the
   upstream signature/attribute — do NOT run the test to discover it.
3. **Version-guarded symbol resolution** — signal: a stub/empty
   implementation surfaces as `KeyError`/`AttributeError` at the first
   real use. Under `vllm_version_is` a name has one definition per branch;
   `next()`-style lookups (AST walks, iterators) can grab the stub. Fix:
   grep ALL `def <name>(` and resolve the ACTUAL implementation (often
   the private method the stub delegates to).
4. **Adapt source and tests together** — when the adaptation changes a
   contract, update the test mocks in the same pass. Verify statically
   (grep the attribute-access chain of the bare object) instead of
   running tests — running is banned and each run costs a full e2e round.

## Accumulated Step Model

The vllm-ascend working tree already contains all successful adaptations from previous steps:
1. Read {previous_step_summary_path} if it exists
2. Reuse prior guards, helpers, imports, and patterns
3. Never revert prior adaptations unless the current change proves them obsolete

The step_target.patch is accumulated (git diff HEAD).

## Code Exploration

- Start from the upstream patch and changed-file list — these are the signal.
  **Key question**: does vllm-ascend subclass, override, call, import, or read
  anything this patch changed? Internal upstream changes only need adaptation
  when vllm-ascend directly depends on the behavior.
- **No-op claims need evidence — "forward is overridden" is NOT evidence.**
  Wrapped upstream modules (shared_experts, token_dispatcher, routed_experts,
  moe_runner) still execute their async/stream/event logic on the Ascend
  wrapper's behalf; a refactor of stream sync / events / ordering / dispatch
  splits can break the wrapper without touching any overridden method.  Query
  `get_adaptation_lessons` for the shared-expert and MoE-gate contract cases
  (L20260819-001/002), and read `{step_dir}/upstream-fix-context.diff`
  (when included in the error logs) before declaring no-op.
- **vllm-report impact map**: {vllm_report_context}
  Call the MCP tools DYNAMICALLY during analysis (see "vllm-report MCP
  Tools" below); guide-returned/guide-empty handling is in Efficiency rule 6.
  Grepping is a fallback for files MCP didn't mention.
- Use the Key Areas in code-structure-guide.md as an
  architecture-level supplement to vllm-report. When vllm-report is
  unavailable (clone failed or commit not covered), use Key Areas to manually
  route changed upstream paths to vllm-ascend files via grep. If codegraph
  tools are available, prefer them; if not, use grep/glob/file reads.
- Read enough of the vllm-ascend code to understand how the subsystem works —
  subclass chains, registration patterns, import structure. Skimming the
  relevant module is better than making a wrong assumption.
- When an upstream change touches an interface that vllm-ascend implements,
  read the upstream base class or caller to understand the contract change.
- **Processor/multimodal trap**: if `changed_files_path` contains
  `vllm/transformers_utils/processors/__init__.py` or `vllm/multimodal/`,
  this is NEVER a no-op. Search vllm-ascend for `*processor*compat*.py` and
  verify every compat patch still works.
  See `reference/adaptation-patterns.md` §12.
- Use `grep` and `glob` to verify that no other vllm-ascend file depends on
  the same changed symbol. When a method signature changes, grep for ALL
  `def <method_name>(` in the vllm-ascend tree — every override must be updated.

## Workflow

### adapt mode

1. Read the upstream patch and changed file list from `{patch_path}` and `{changed_files_path}`
2. Read the vllm-report impact map in Code Exploration above FIRST, then use targeted search to verify and fill gaps
3. Apply minimal changes — do not refactor unrelated code
4. **Apply the Format rules and mypy prevention rules below WHILE editing**
   (format + mypy are NOT checked per-step - they run once at push time on
   the accumulated diff. Apply the rules BY READING as you edit — NEVER run
   mypy/ruff/py_compile to check (banned, see Rules above). If the final
   gate fails, the exact `file:LINE:CODE` is fed back via
   `quality_gate.json` in fix mode).
   - **Ignore env noise in format output**: gitleaks "is not executable",
     shellcheck missing, "Exec format error" are infrastructure issues.
     Do NOT modify `.github/workflows/scripts/` to fix these.


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

1. Every `vllm_version_is` guard: NEW upstream-main code is in `else`/`not`
   branch, OLD release code is in `if` branch.
2. Every guarded `from vllm.X import Y` line has `# type: ignore[import-not-found]`
3. Imports that don't exist on the OLD vllm version: the import of the new
   class MUST be inside `else` (not guarded with `# type: ignore`).
4. No circular imports
5. Every call site passes correct number, type, AND ORDER of arguments on
   BOTH version branches. Use keyword arguments for new parameters.
6. Override methods match the upstream signature.
7. **Base-class attribute sync**: when upstream adds an attribute to a base
   class, EVERY vllm-ascend subclass must accept and set it — base-class
   code reads `self.X` at runtime, and "GPU-only" is NOT a reason to skip.
   See `reference/adaptation-patterns.md` §9.
8. No variable aliases as base classes — use `TypeAlias` or direct class name.
9. When fixing a version-branch bug, grep for the same pattern in ALL sibling
   functions and fix them all in the same commit.
10. When a method signature changed, grep for ALL `def <method_name>(` in the
   codebase — every override must be updated.
11. Every `next(gen, default)` has a default value — no bare `next(...)`.
12. `super().__init__()` called in every subclass `__init__`.
13. No exact version matching (`== "X.Y.Z"`).
14. No dead code, commented-out blocks, or stale `# type: ignore` left behind.
15. See `reference/common-pitfalls.md` §"Additional QA-level checks" for
    remaining items (registries, Triton params, getattr, path resolution, etc.).
16. **Return type change → grep EVERY `return` in the method**: one
   leftover old-type return slips past pre_ci and mypy.  See
   `reference/common-pitfalls.md` §"Return type mismatch across version
   branches".
17. **Conditional method definition**: the `else` branch (new signature)
   MUST carry `# type: ignore[misc]`.  See
   `reference/adaptation-patterns.md` §13.
18. **Triton kernel signature match**: patched kernel MUST match the
   upstream call site exactly (validated at launch, not definition).  See
   `reference/adaptation-patterns.md` §14.
19. **`device_index` passed explicitly**: NPU device APIs (e.g.
   `npu_generate_uuid()`) take `device_index` from `self.device.index`,
   not the ambient current device.  See `reference/common-pitfalls.md`
   §"`device_index` must be passed explicitly (not ambient)".
20. **No variable name shadowing**: new local variables must not shadow
   names in enclosing scopes; grep the file for the name before
   introducing it.  See `reference/common-pitfalls.md` §"Variable name
   shadowing".

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

The working tree already contains the failed adaptation — do NOT start from
scratch. Fix ONLY what the error says (Efficiency rule 8).

**Pre-CI failures**: open `pre_ci_check.json` → `violations` carry exact
file:line:col:CODE. Fix those specific lines.

**E2E test failures**: open `round-N-result.json` → if `code_bugs_count` > 0,
open failed tests from `suite_results[test_name]`. Read both `-summary.json`
(structured code_bugs/env_flakes) and `.log` (raw traceback):
1. Read the FULL traceback first — identify the exact failing path (normal
   vs cache, with-data vs no-data, batch vs single). Do NOT guess.
2. **MUST call `get_adaptation_lessons(keywords=["<error message / test
   name>"])` BEFORE fixing.** Follow its fix_guidance directly — skipping
   may repeat a documented mistake. (If the MCP call fails, log and continue.)
3. Check `reference/common-pitfalls.md` for a KNOWN failure with this exact
   error message; follow its fix requirements if one matches.
4. **Multi-path check (the #1 reason E2E fixes fail on first attempt)**:
   does the patched function get CALLED on the failing path? Does the fix
   cover ALL paths reaching the asserted invariant, or just the one you
   looked at? See `reference/common-pitfalls.md` §"Fix covers only ONE of
   multiple code paths".

**ImportError is NOT an env flake** - it is a real adaptation gap. When E2E
fails with `ImportError: cannot import name 'X' from 'Y'` from a pinned dep
(triton, torch, etc.) where X is newly referenced by vllm main, add a compat
stub in `vllm_ascend/__init__.py` (module-level, before vllm imports) — see
`reference/common-pitfalls.md` §"Environment compatibility stubs"
(triton.experimental.gluon case, PR #13137). Do NOT mark no-op/env-flake.
If the stub imports a module without `py.typed` (e.g. triton), add
`# type: ignore[import-untyped]` to the import.

**Final quality gate failures (push-time format + mypy)**: after all steps,
format + mypy run once on the accumulated diff.  If they fail, `error_logs`
contains `quality_gate.json` (NOT `pre_ci_check.json`):
```json
{{"all_passed": false, "checks": [
  {{"name": "format", "violations": ["file.py:LINE:CODE ..."]}},
  {{"name": "mypy",  "violations": ["file.py:LINE:COL: error: ... [override]"]}}
]}}
```
Fix format by `file:LINE:CODE` (E501 break line, F401 delete import); fix
mypy per error code — see `reference/common-pitfalls.md` §"mypy error
codes". Mechanical fixes — do NOT re-analyze the upstream patch. After
fixing, e2e re-runs; if it fails, the fix regressed — revert and retry.

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

**Cause vs Change — they must be DIFFERENT:**
- **Cause** = what the upstream vLLM commit changed
- **Change** = what vllm-ascend did to adapt

Multi-line fields: indent continuation lines with 2 spaces.

Do NOT write the same text for both fields.

## Last Step Only

If {is_last_step}: check code-structure-guide.md freshness. If stale, write updated version as {step_dir}/{code_structure_guide_file}.

## vllm-report MCP Tools (on-demand)

A vllm-report MCP server is registered. You can call these **read-only** tools
during adaptation to query deeper information not in the injected impact map.

### How to use MCP tools - decision flow

```
1. START HERE — call MCP tools BEFORE grepping:
   ├─ get_adaptation_guide(sha=<end_commit>) FIRST — if it returned data,
   │  follow it (Efficiency rule 6); if empty, analyze the diff yourself.
   ├─ get_cross_project_mapping() — vllm path -> ascend file map; call it
   │  before deciding which files to grep.
   └─ Only skip grep when MCP data exists; otherwise grep is your analysis.
2. HOW to adapt a specific change? → get_adaptation_guide (already called),
   get_patch_catalog(category="platform"|"worker"), search_analysis(...).
3. Understand a subsystem? → get_key_abstractions(repo="vllm-ascend"),
   get_module_info(repo, module_name), get_development_workflows().
4. In fix mode, why did a test fail? → search_analysis(keywords=[...]),
   get_commit_arch_delta(repo="vllm", sha=<end_commit>).
```

**CRITICAL**: Call MCP tools FIRST, before any grep. The tools return
authoritative impact maps extracted from vllm-ascend's actual patch wiring.
Grepping is a FALLBACK for files the MCP tools didn't mention — don't
grep for files that MCP already identified. Typical savings: 31 greps → 3-4
MCP calls + 2-3 targeted greps (for gaps).

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

- MCP tools are SUPPLEMENTARY to `{vllm_report_context}`. The injected impact
  map already covers the basics. Call tools only when you need deeper info.
- Do NOT call `update_adaptation_status` or `advance_baseline` - these are
  write tools. main2main is a read-only consumer.
- Limit to 2-3 tool calls per step. Each call takes ~1-2s; don't over-query.
- If a tool call fails or times out, fall back to grep/file reads immediately.
- When vllm-report says "no ascend impact" but grep finds a base-class change,
  trust grep (vllm-report may not have analyzed this commit).

## Reference (read on demand)

The reference docs below are NOT inlined — carrying them in the prompt makes
every tool-call generation slower. Read a file ONLY when the current question
depends on it, and read only the relevant section (grep the file for the
heading first, then read that range).

{reference_content}