# AGENTS.md

Main2Main flow that automates vllm-ascend's main2main upgrade against upstream vLLM. Drives an external `opencode run` subprocess as the AI adapter; everything else is deterministic Python.

## Run

Install once: `pip install -e .`

Real entrypoint is `Main2MainFlow` in `main2main_flow/flow.py`. Install and run:

```bash
pip install -e .
kickoff --vllm-path <path|url> --vllm-ascend-path <path|url> [--target-commit SHA]
```

Both repos must be real git checkouts (or HTTPS URLs that will be cloned into `workspace/repos/`). vllm HEAD is the implicit target unless `--target-commit` is given.

## Layout

- `main2main_flow/flow.py` — the Flow; node order: `initialize → _warmup_mega_moe → analyze_commit_and_plan_step → process_steps (per-step _ai_analysis + _run_e2e_test, then _final_quality_gate with the wide gate e2e + release smoke) → generate_final_post → persist_lessons → push_to_github`. Routing uses string signals defined in `scripts/utils/utils.py` (`HasCommit`, `HasNoCommit`, `UpgradeCompleted`, `UpgradeFailed`). Two early exits: `HasNoCommit` (nothing to adapt) and `current_step == 0` after process_steps (no PR — nothing passed e2e).
- `main2main_flow/cli.py` — CLI entry point (`kickoff`).
- `main2main_flow/agents/` — agent SKILL.md files and per-role reference docs consumed by opencode. Each role is a self-contained directory:
  - `adapter/SKILL.md` + `adapter/reference/` — adapt and fix modes (MCP is PRIMARY, grep is FALLBACK)
  - `adapter-qa/SKILL.md` + `adapter-qa/reference/` — independent reviewer
  - `description-fill/SKILL.md` — read-only analysis for PR-description file attribution
- `main2main_flow/scripts/agent/opencode_adapter.py` — spawns `opencode run --format json --dangerously-skip-permissions`, streams JSONL, 30 min total / 5 min stale timeouts, supports `--session` for persistent sessions.
- `main2main_flow/scripts/utils/` — deterministic helpers and shared utilities:
  - `utils.py` — filename constants, git helpers, `ts_print`
  - `detect_commits.py`, `plan_steps.py` — commit detection and planning (impact routing via vllm-report MCP `get_commit_impact_batch`)
  - `commit_ref.py` — replace the pinned verified-commit SHA across tracked vllm-ascend files
  - `pre_ci_check.py` — per-step: version strings, temp files, format, broken imports (module + symbol against BOTH vllm trees: main + pinned release worktree), mypy (main 3.10/3.11/3.12 + release 3.10), CPU-UT (main batch); a missing release worktree emits a skipped `release_lane` entry instead of failing
  - `final_quality_gate.py` — push-time gate: format + mypy (both vllm trees) + CPU-UT (`ut_check.py`, main batch + release batch with `VLLM_VERSION=<tag>`, known-failure baseline allowlist; per-file isolation with fake npu-smi)
  - `release_ut_baseline.json` — CPU-UT node IDs known to fail on the release lane independent of the current adaptation (never block; `MAIN2MAIN_RELEASE_UT_BASELINE=0` shows all)
  - `run_tests.py` — e2e test runner with two schedulers: `rolling` (default) dispatches a suite the moment enough cards are free and re-dispatches on every completion — no round barrier for idle cards to wait behind; dispatch priority is LPT (more cards, then longer estimate, first), or the caller's order under `preserve_order=True`. `rounds` is the legacy batch scheduler (`MAIN2MAIN_TEST_SCHEDULER=rounds` restores it). Device-overriding tests get an exclusive reserved window (their hardcoded physical 0..N-1); pair alignment still applies
  - `extended_e2e.py` — gate main-e2e case selection: the fixed curated `extended_e2e` policy key (runtime guards against allowlist drift/upstream skip_tests/`_310p` suites/missing files), the `MODE=full` tree-scan resolver (`tests/e2e/pull_request/**/test_*.py` minus `skip_tests`/fixed-set/`_310p`/blocklist), and import-closure tiering + estimated-time ordering
  - `push_to_github.py` — push branch + create PR + add labels
  - `ci_log_summary.py` — test log parsing
  - `lessons.py` — submit/persist adaptation lessons to vllm-report
  - `track_pr_ci.py` — PR CI result tracking (vllm-report `daily_refresh.sh` step 10; kept in sync with the vllm-report copy)
  - `pr_ci_monitor.py` — post-push closed loop: polls the PR's check-runs, classifies failures, and repairs the PR (rebase conflicts deterministically, infra via rerun, content via own-diff triage + adapter-fix)

## workspace/ is volatile

`initialize` **deletes and recreates** `workspace/` on every run. Never put anything there you want to keep. All step artifacts (`workspace/steps/<step-id>/upstream.patch`, `step_summary.md`, `step_target.patch`, `opencode.log`, `opencode_raw.jsonl`, `tests/round-*-result.json`, `pre_ci_check.json`) live under it. Filenames are centralised as constants in `scripts/utils/utils.py` — reuse them, don't hardcode strings.

## State & path constants

- `WORKSPACE_DIR = <repo>/workspace` (computed from `__file__`, respects `MAIN2MAIN_WORKSPACE` env var).
- Path resolution priority in `initialize`: CLI arg → env var (`VLLM_PATH`, `VLLM_ASCEND_PATH`, `VLLM_TARGET_COMMIT`) → default. URLs starting with `http(s)://` or `git@` are auto-cloned; existing target dirs get **removed first**.
- `initialize` records `original_vllm_ref` / `original_ascend_ref` and `generate_final_post` checks them back out (`-f` for ascend). If you add new checkouts/branch switches mid-flow, make sure restoration still works.

## Retry & test loop semantics

`process_steps`: per step, run `_ai_analysis` then `_run_e2e_test`. Pass → next step, reset `retry_count`. Fail → `retry_count++` and re-enter `_ai_analysis` in fix mode. At `retry_count >= 3` the flow short-circuits to `UpgradeFailed`. Two skips:
- adapter-declared **no-op** steps (`is_noop`, first attempt only) skip the per-step e2e and just commit the verified.commit bump
- a **0-step run** (`current_step == 0` after process_steps) skips PR creation entirely

Inside `_ai_analysis`, the attempt loop (up to 3×):
1. **adapter** (role=adapter) — generates adaptations, consults vllm-report via MCP
2. `run_check` — per-step pre-CI: version_strings, temp_files, broken_imports, format, and (when a vllm checkout is available) mypy + CPU-UT
3. **adapter-qa** — independent AI review (separate opencode session, no generator context)
4. All pass → break. Any fail → retry with **adapter-fix** (role=adapter-fix, with error_logs inlined).

Every e2e run (per-step, gate, release smoke) uses the same ROLLING
scheduler by default (2026-09-16): a suite launches the moment enough
cards are free and the next fitting suite starts as soon as devices are
released — idle cards never wait behind the slowest suite of a batch
(run 34994732545's gate e2e: five barrier rounds turned ~20 min of
device-time into 42 min of wall).  `MAIN2MAIN_TEST_SCHEDULER=rounds`
restores the batch scheduler.

The same checks re-run once at push time in the final quality gate
(`final_quality_gate.py`) on the CUMULATIVE diff, fixing failures via
adapter-fix (max 5 rounds) and re-running e2e to confirm no regression.
The gate's UT runs TWO batches: main tree, plus the pinned release worktree
with `VLLM_VERSION=<tag>` (release-lane failures matching
`release_ut_baseline.json` never block).  `MAIN2MAIN_UT_GATE=0` disables UT
in the gate.

After the main-lane e2e is green, the gate also runs a release-tag e2e
SMOKE (8 nodes from `test_policy.json` `release_smoke`, ~12min): run_tests
with `skip_setup=True` and PYTHONPATH pointing at the release worktree +
`VLLM_VERSION=<tag>` — the only check that executes the release lane's
engine lifecycle (the PR-CI release e2e leg is main2main-label-gated, so
flow PRs are the only per-PR executors).  A lane probe (tree identity +
version resolution) runs first; a mis-resolved lane skips loudly instead
of burning NPU time on a meaningless run.  The dflash and dspark-w4a8
spec-decode nodes were added 2026-09-15 after PR 16575's dflash/dspark
release-lane break (main-only `_build_uniform_attn_metadata` called
unconditionally) was invisible to the previous 4-node smoke.  A failed
smoke retries once on
the same tree (flake), then is triaged: traceback files inside the
adaptation diff → own-diff → adapter-fix round (shared 5-round budget);
files entirely outside the diff → upstream-inherited (the PR #16382
shape) → recorded in `quality_gate/release_smoke_inherited.json`, not
blocking.  Remote e2e mode self-skips (the container can't see the local
release worktree).  `MAIN2MAIN_RELEASE_TEST_CASES` overrides the case
list; empty list disables the smoke.

After the statics are green, the gate also runs the WIDE main-lane e2e
(`_run_gate_e2e`) — since 2026-09-15 the gate's regression e2e IS the
extended coverage set (the separate post-gate phase is gone; user
decision).  Trigger unchanged: the last step skipped/failed its per-step
e2e, or gate fixes edited the tree.  The per-step set only proves the
cases it contains (PR 16575 shipped green on pre_ci while upstream CI
failed legs it never touched).

Case source (`extended_e2e.py`): by default the FIXED curated
`test_policy.json` `extended_e2e` key — 36 cases (2026-09-16 trim of the
55-case set; 24 one-card, 7 two-card, 5 four-card) weighted
toward the surfaces that recently failed CI (extract_hidden_states,
gumbel, mamba, Kimi-K3 DSpark, DSparkSpeculator, PCP), and excluding
every `_310p` suite (the a3-16 pool absolutely
cannot run 310P hardware paths — the exclusion is structural, applied at
runtime in BOTH modes, overrides included).
Curation is offline; runtime guards drop entries the per-step allowlist
already covers (drift), upstream `skip_tests` has since claimed, `_310p`
suites, or files missing from the tree.
`MAIN2MAIN_EXTENDED_MODE=full` opts into the whole-label resolver instead
(tree scan of `tests/e2e/pull_request/**/test_*.py` − `skip_tests` −
fixed-set coverage − `_310p` − blocklist, hours).  In both modes, cases
whose test file imports a module touched by the adaptation diff run first
(import-closure tier); the rest follow, both by estimated time ascending
(`run_tests` is called with `preserve_order=True`).

Failure handling is the release-smoke triage applied to the wide set: one
delta re-run of the failed suites absorbs flakes; traceback files
entirely outside the adaptation diff → upstream-inherited (the PR 16382
shape) → recorded in `quality_gate/gate_e2e_inherited.json`, not
blocking; own-diff failures enter adapter-fix rounds
(`MAIN2MAIN_EXTENDED_FIX_ROUNDS`, default 2, re-running ONLY the failed
suites) with a wall-clock backstop (`MAIN2MAIN_EXTENDED_MAX_MIN`, default
360) and a stop-loss on an identical failing set; pytest exit 4
(collection error) suites are excluded from fix rounds permanently (when
only they remain, the triage verdict decides).  Each fix round re-checks
the gate statics when the tree changed.  Unlike the old post-gate phase
this runs INSIDE the gate: exhausting the budgets on an own-diff failing
set FAILS the gate — upstream CI verifies the inherited risk, not a
proven own regression.  Fixes stay uncommitted (uniform with every other
gate fix); the gate's success path regenerates `gate_final_patch` from
the working tree.  Evidence: `quality_gate/gate_e2e_result.json` + a
"Gate e2e" section in `final_summary.md`/the PR body.
`MAIN2MAIN_EXTENDED_E2E=0` escapes to the LEGACY 23-case regression e2e
(`_run_e2e_test_for_final_gate`: same-tree flake retry + revert on
determinism).  The wide e2e is memoized by tree sha (a green or
inherited-suppressed tree is not re-run until the tree changes), as are
the statics and the smoke.

## PR CI closed loop

After `push_to_github` creates the PR, `_monitor_pr_ci` (`MAIN2MAIN_PR_WATCH`,
default on) runs the watcher (`pr_ci_monitor.watch_and_fix`): it polls the
PR's check-runs until terminal, then repairs the PR within budgets.  The
upstream CI stays the only test executor — the watcher runs no tests itself.

- **rebase conflict / CSRC drift** (cpu-ut/select-tests logs) — deterministic
  `git rebase` onto upstream main; conflicts go through one adapter-fix round
  (conflict files as error_logs), then lease-push + `main2main_baseline`
  write-back (the daily bot force-pushes the branch, so without the baseline
  write-back a fix would be ephemeral).
- **infra-shaped failures** (no test-failure signature in the job log) —
  `gh run rerun --failed`, budgeted.
- **content failures** — own-diff triage: root-cause files inside the
  adaptation diff → adapter-fix rounds (same payload contract as the gate,
  error_logs = fetched job logs); entirely outside it → upstream-inherited
  (#16382 shape), evidence + PR comment only.  An adapter-declared
  `env-flake` triggers a rerun instead of a push.
- `ci-gate` is an aggregator and never actionable by itself; exhausted/
  timeout/inherited endings post a deterministic PR comment.  Evidence:
  `workspace/pr_ci_watch/round-N/` + `pr_ci_watch_result.json`.
- Every watcher failure is recorded, never propagated — the run itself has
  already succeeded by the time it executes.

## Env flags worth knowing

| Var | Effect |
|---|---|
| `SKIP_AI_ANALYSIS=true` | Bypass opencode entirely; only deterministic ops run. Useful for debugging the Flow plumbing. |
| `SKIP_E2E_TEST=true` | `_run_e2e_test` returns True without touching NPU. |
| `PUSH_TO_GITHUB=true` + `GITHUB_REPO=owner/name` | Enables `push_to_github`; requires `gh` logged in. |
| `HEAD_FORK=org/name` | Fork repo to push to (default: `vllm-ascend-ci/vllm-ascend`). |
| `MAIN2MAIN_MODEL=provider/model` | opencode model (default: `deepseek/deepseek-flash`). Per-role overrides: `MAIN2MAIN_MODEL_ADAPT`, `MAIN2MAIN_MODEL_FIX`, `MAIN2MAIN_MODEL_REVIEW`. |
| `MAIN2MAIN_TIMEOUT_MIN` | opencode total timeout minutes (default: 30). |
| `MAIN2MAIN_STALE_SEC` | opencode stale timeout seconds (default: 300). |
| `MAIN2MAIN_WORKSPACE` | Workspace root directory (default: `<repo>/workspace`). |
| `MAIN2MAIN_TEST_CASES` | Space-separated test paths to run. |
| `MAIN2MAIN_RELEASE_TEST_CASES` | Space/newline-separated e2e nodes for the gate's release-tag smoke (default: `test_policy.json` `release_smoke` key; empty disables the smoke). |
| `MAIN2MAIN_KEEP_BRANCH` | Skip `git reset --hard origin/main` in vllm-ascend setup. |
| `PR_LABELS` | Comma-separated labels for created PR (default: `main2main`). |
| `PR_DRAFT` | Create draft PR (default: `true`). |
| `MAIN2MAIN_UT_GATE` | `0` disables UT in the final quality gate (default: `1`). |
| `MAIN2MAIN_RELEASE_GATE` | `0` skips all release-lane validation: no release worktree use in pre_ci/gate (default: `1`; the worktree is still built). |
| `MAIN2MAIN_RELEASE_UT_BASELINE` | `0` disables the `release_ut_baseline.json` allowlist — release-lane UT failures then block (default: `1`). |
| `MAIN2MAIN_RUN_TESTS_REMOTE` | Run tests on a remote host via SSH (`user@host` or `env`). |
| `MAIN2MAIN_REMOTE_HOST`, `MAIN2MAIN_REMOTE_CONTAINER` | SSH host and container for remote e2e tests. |
| `MAIN2MAIN_PR_WATCH` | `0` disables the post-push PR CI closed loop (default: `1`). |
| `MAIN2MAIN_PR_WATCH_TIMEOUT_MIN` | Overall watcher budget in minutes (default: `360`). |
| `MAIN2MAIN_PR_WATCH_ROUND_TIMEOUT_MIN` | Per CI round (one head sha) poll timeout (default: `200`). |
| `MAIN2MAIN_PR_WATCH_FIX_ROUNDS` | Adapter-fix/rebase fix rounds (default: `2`). |
| `MAIN2MAIN_PR_WATCH_RERUNS` | Infra rerun budget (default: `2`). |
| `MAIN2MAIN_PR_WATCH_POLL_SEC` | Check-run poll interval seconds (default: `300`). |
| `MAIN2MAIN_PR_WATCH_BASELINE` | `0` skips the `main2main_baseline` write-back after an accepted fix (default: `1`). |
| `MAIN2MAIN_PR_WATCH_COMMENT` | `0` skips the PR comment on exhausted/timeout/inherited (default: `1`). |
| `MAIN2MAIN_EXTENDED_E2E` | `0` reverts the gate's main e2e to the legacy 23-case regression set (default: `1` runs the wide 36-case set with triage). |
| `MAIN2MAIN_EXTENDED_MODE` | `fixed` (default) runs the curated `test_policy.json` `extended_e2e` set (~17min); `full` resolves the whole label minus subtraction (hours). |
| `MAIN2MAIN_EXTENDED_FIX_ROUNDS` | Adapter-fix rounds for extended e2e failures after the first round (default: `2`). |
| `MAIN2MAIN_EXTENDED_MAX_MIN` | Extended e2e wall-clock backstop in minutes, `0` = off (default: `360`). |
| `MAIN2MAIN_EXTENDED_TEST_CASES` | Space/newline-separated extended e2e cases replacing both sources entirely (drift guards still apply). |
| `MAIN2MAIN_EXTENDED_INCLUDE_SKIPPED` | `1` keeps upstream `skip_tests` files in the `full`-mode set (default: `0`; no effect in `fixed` mode). |
| `MAIN2MAIN_TEST_SCHEDULER` | `rounds` restores the legacy round-barrier e2e scheduler (default: `rolling` — suites dispatch as devices free up). |
| `MAIN2MAIN_MYPY_VENV` | Persistent mypy lint venv directory (default: `<workspace>/mypy_venv`; built once, rebuilt when triton-ascend's numpy constraint moves). |

## Conventions

- Python 3.10–3.13. Uses `uv`. The `.venv/` at repo root is the uv venv — don't recreate.
- No lint/typecheck/test commands are wired up. Verify changes by `SKIP_E2E_TEST=true SKIP_AI_ANALYSIS=true kickoff ...` against a small synthetic commit range.
- All adapter outputs that need persistence go through `scripts/utils/utils.py` filename constants; introducing a new artifact means adding a constant there first.
- vllm-ascend version guards must use `vllm_version_is("{release_tag}")` exactly — `pre_ci_check` will reject any new `vllm_version_is(...)` call whose tag doesn't match `state.release_tag`.

## Don'ts

- Forget to `pip install -e .` after restructuring the code.
- Don't keep `workspace/` paths between runs; they vanish on `initialize`.
- Don't add `{var}` placeholders to SKILL.md that aren't passed into `_build_prompt`'s ctx dict, or `format_map` will KeyError.
- Don't commit anything under `workspace/`, `output/`, `.venv/`, or `.env` (already gitignored).
