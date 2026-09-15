# Upstream Contract Drift — the standard adaptation playbook

Read this when pre_ci failures form a FAMILY: many UT violations in one
subsystem, or one attribute/method name failing across several files.
That shape means an upstream commit changed a shared contract — not N
independent bugs. Fixing the family line-by-line never converges within
the round budget (run 33976675052 step-1: 41 → 8 → 9 UT failures over
3 rounds, then killed).

## Index

| Section | Trigger — read when... | Lines |
|---------|------------------------|-------|
| §1 Recognition signals | deciding whether this playbook applies | 22-34 |
| §2 Read the contract source first | starting the fix | 35-44 |
| §3 Build the old→new mapping table | before editing anything | 45-60 |
| §4 Family grep — source AND tests | you know the removed/renamed symbols | 61-74 |
| §5 Fix top-down | editing | 75-84 |
| §6 Close the loop from evidence | after each edit batch | 85-101 |
| §7 Worked example | you want to see the whole playbook applied | 102-118 |
| §8 Release-lane dataclass field drift | release lane: `TypeError: __init__() missing N required positional argument(s)` (main green) | 120-147 |
| §9 Silent metric degradation — no own frames | e2e assert/golden failure whose traceback has NO vllm_ascend frame | 147-end |

## 1. Recognition signals

- UT failures cluster in one subsystem (e.g. KV pool / MLA / model_runner)
  and their count is >3.
- The same AttributeError/TypeError name repeats across files
  (`has no attribute 'compress_ratio'` in 6 tests = 1 contract change).
- mypy errors point at the same fields the UT errors do.
- The failing symbols exist in the release version but not in the target
  upstream checkout (or vice versa) — check the upstream patch, not the
  error line.

If ≥2 signals hold, STOP reading individual tracebacks and do §2.

## 2. Read the contract source first

Open the NEW definition in the upstream checkout (`{vllm_path}`) — the
dataclass/class/module that now owns the contract — NOT the failing test
line. The upstream commit message + diff (`{patch_path}`) tells you which
files define the new contract. Understand:

- what was removed/renamed (old symbols), and
- what replaces it (new symbols, their types, and their invariants).

## 3. Build the old→new mapping table

Write it into `analysis.md` BEFORE editing — one row per symbol:

```
| old (release)                | new (upstream main)                  |
|------------------------------|--------------------------------------|
| KVCacheTensor.shared_by      | KVCacheTensor.layers                 |
| MLAAttentionSpec.compress_ratio | MLAAttentionSpec.tokens_per_state |
| spec.indexes_kv_by_block_stride | (removed — derived from layout)   |
```

This table is the whole fix: every edit is a mechanical application of a
row. If you cannot fill a row, grep the upstream definition again — do
not guess (a wrong mapping produces a NEW family of failures).

## 4. Family grep — source AND tests

For EACH old symbol in the table:

```bash
rg -n "<old_symbol>" {ascend_path}/vllm_ascend {ascend_path}/tests/ut
```

Both trees. An adaptation that changes a contract invalidates the test
mocks that encode the old one — updating only `vllm_ascend/` guarantees
the next round fails in `tests/ut` instead (the most common
round-to-round failure shape). Grep sibling overrides of the same method
too (SKILL.md checklist 9-10).

## 5. Fix top-down

1. Definitions (dataclasses/classes) — so constructors type-check first
2. Constructors/factories and their callers
3. Call sites (order from the family grep)
4. Test mocks/fixtures

Bottom-up (patching each failing test first) churns: every level you fix
afterwards re-fails the tests above it.

## 6. Close the loop from evidence

You cannot run tests in-session (the guard blocks them) — you close the
loop by making every failure IMPOSSIBLE to recur, from evidence:

- Read the FULL tracebacks in the pre_ci UT log (`log_path` in
  `pre_ci_check.json` → `checks` → `ut`) — not just the excerpts. Each
  traceback names one call site; map it to a row in the §3 table.
- For each old symbol, re-grep after editing until ZERO references
  remain in BOTH `vllm_ascend/` and `tests/ut/`. Stopping with 2 of 9
  call sites updated is the #1 reason a family persists across rounds.
- Stop and RE-ANALYZE (§2) if a traceback doesn't fit your mapping
  table — that means a row is wrong, not that the edits are incomplete.
- Do mypy-relevant hygiene (guards, `# type: ignore[import-not-found]`)
  as you edit, but do not chase residual mypy errors — contract-aligned
  code makes most of them vanish on their own.

## 7. Worked example — KV-Cache Layout Refactor (#51718, step vllm 8bdc70ec)

Upstream standardized KV cache layout: `KVCacheLayout` enum;
`MLAAttentionSpec` lost `compress_ratio`/`indexes_kv_by_block_stride` and
gained `tokens_per_state`/`num_states`; `KVCacheTensor.shared_by` became
`.layers`; `model_runner_v2` builds spec attrs via `SimpleNamespace.layers`.

Wrong path (what killed the step): 3 rounds of editing exactly the line
each traceback named — round 1 fixed one spec construction, round 2
uncovered the next call site, round 3 fixed tests the round-2 edits
broke. 41 → 8 → 9 failures, budget exhausted.

Right path by this playbook: §2 read the new `KVCacheTensor`/`MLAAttentionSpec`
definitions upstream → §3 the 3-row table above → §4 one `rg` per old
symbol over `vllm_ascend/ tests/ut/` (~9 files total) → §5 definitions,
then constructors, then call sites, then mocks → §6 zero-reference grep
per old symbol. Convergence in 1-2 rounds.

## 8. Release-lane dataclass field drift

Trigger: the release lane fails with
`TypeError: __init__() missing N required positional argument(s): '<field>'`
(e2e) or `Missing positional argument "<field>" ... [call-arg]` (the
release mypy pass), while the main lane is green.  The vllm-ascend
subclass already carries SOME compat fields for this class — the error is
the ONE you haven't heard of yet, and it is never alone.

Playbook (this is §3/§4 specialized to dataclasses):

1. Read the base dataclass in BOTH trees — the pinned release worktree
   (label `release(...)` in mypy details) and the main checkout.  Diff the
   field lists, including which fields lack defaults.
2. Grep the release tree for the base class's required fields — there is
   NEVER just one drifted field; upstream adds them in batches
   (`max_seq_len_np` came with siblings).  Map every field that differs.
3. Fix by the SUBCLASS pattern: the vllm-ascend subclass owns each compat
   field as `field(default=None, kw_only=True)` and every construction
   site passes it by name.  See `common-pitfalls.md` §"Dual-version
   dataclass fields" for the full four-axis matrix and the three rejected
   patterns (plain subclass default = release-tree import-time TypeError;
   inline conditional kwargs = main mypy; runtime kwargs dict = AST
   structure tests).
4. Do NOT write the fix against one direction ("release added it") —
   verify both trees and keep the write direction-symmetric.

## 9. Silent metric degradation — no vllm_ascend frame in the traceback

Trigger: an e2e failure that is an assert on a computed metric (spec-decode
`acceptance_per_pos` below golden, token-distribution mismatch, accuracy
below threshold) whose traceback names NO vllm_ascend file — only the vllm
test and its assert. No crash, no EngineDeadError, often identical numbers
across compilation modes. PR #16554 (vllm 62f3bf58→39545e47): dflash pos0
0.39 vs golden 0.51, dspark ~halved at every position, eagle/MTP green.

**The log cannot root-cause this failure** — the evidence is not in it.
The cause is an OMISSION in the adaptation, and three facts locate it:

- The vllm-ascend diff for the failing surface can be innocent-looking
  (+2/-1 mixin inheritance) — the bug is something that is NOT there.
- Upstream usually got here by RENAMING a base method AND DELETING a
  subclass override. A deleted override's body is a contract callers
  relied on: injected arguments, asserts, early returns.
- A NEW mixin/base class inserted in FRONT of the subclass (MRO) silently
  changes which override answers legacy call sites — the forwarding
  shim's DEFAULTS replace the deleted injections.

Bounded path — 4 commands, each one decisive; do not broaden the search:

1. Map the failing surface to its vllm-ascend file(s) and read the
   vllm-ascend side of the diff (`git diff` in {ascend_path}) — note any
   class whose MRO gained a new mixin/base in this step.
2. Find what upstream DELETED: grep the step patch (`{patch_path}`) for
   `-    def <method>(` blocks in the upstream file that owns the
   surface; if the deletion predates this step's range,
   `git -C {vllm_path} log/diff <older-sha>..<newer-sha> -- <file>`.
   Each deleted body = enumerate its implicit contract items.
3. Grep the vllm-ascend call sites that used to route through the deleted
   override; for each contract item decide: re-implement it in the shim,
   or pass it explicitly at every call site (PR #16554 fix:
   `num_query_per_req=self.num_query_per_req` at both
   `build_draft_attn_metadatas` call sites — the shim had defaulted it
   to 1, so draft metadata claimed 1 query token/req instead of 8/7,
   and the Ascend `actual_seq_lengths_q` patch hid the tiling crash,
   leaving silently wrong attention).
4. Verify against the METRIC, not against "no crash" — silently wrong
   attention passes every smoke that only checks termination. State in
   `analysis.md` which contract item each edit restores.

If step 2 shows no deletion, this is NOT §9 — go to §2 and re-read the
new contract source. Timebox: the whole path is ≤4 tool calls; a session
that improvises here instead is the run-34018086282 shape (80min, killed).
