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
