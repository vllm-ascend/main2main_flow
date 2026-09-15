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

- `main2main_flow/flow.py` — the Flow; node order: `initialize → _warmup_mega_moe → analyze_commit_and_plan_step → process_steps (per-step _ai_analysis + _run_e2e_test, then _final_quality_gate, then _run_extended_e2e) → generate_final_post → persist_lessons → push_to_github`. Routing uses string signals defined in `scripts/utils/utils.py` (`HasCommit`, `HasNoCommit`, `UpgradeCompleted`, `UpgradeFailed`). Two early exits: `HasNoCommit` (nothing to adapt) and `current_step == 0` after process_steps (no PR — nothing passed e2e).
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
  - `run_tests.py` — e2e test runner with parallel scheduling (`preserve_order=True` hands the caller's case order to the scheduler untouched; default LPT re-sort unchanged)
  - `extended_e2e.py` — post-gate extended e2e case resolution: tree scan of `tests/e2e/pull_request/**/test_*.py` minus upstream `skip_tests`/fixed-set coverage/blocklist; import-closure tiering against the adaptation diff + estimated-time ordering
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

The same checks re-run once at push time in the final quality gate
(`final_quality_gate.py`) on the CUMULATIVE diff, fixing failures via
adapter-fix (max 5 rounds) and re-running e2e to confirm no regression.
The gate's UT runs TWO batches: main tree, plus the pinned release worktree
with `VLLM_VERSION=<tag>` (release-lane failures matching
`release_ut_baseline.json` never block).  `MAIN2MAIN_UT_GATE=0` disables UT
in the gate.

After the main-lane e2e is green, the gate also runs a release-tag e2e
SMOKE (6 nodes from `test_policy.json` `release_smoke`, ~10-15min): run_tests
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

After the gate passes, `_run_extended_e2e` runs the FULL main2main-label
e2e set — what upstream PR CI would execute for this PR — while the tree
is final and the NPUs would otherwise idle until push.  The fixed 25-case
policy set only proves the cases it contains (2026-09-15: PR 16575 shipped
green on pre_ci while upstream CI failed legs the fixed set never touched).
Case source (`extended_e2e.py`): tree scan of `tests/e2e/pull_request/**/test_*.py`
− upstream `test_config.yaml` `skip_tests` − fixed-set coverage (file/node)
− policy blocklist − missing files.  Cases whose test file imports a module
touched by the adaptation diff run first (import-closure tier); the rest
follow, both tiers by estimated time ascending (`run_tests` is called with
`preserve_order=True`).  Failures enter adapter-fix rounds
(`MAIN2MAIN_EXTENDED_FIX_ROUNDS`, default 2, re-running ONLY the failed
suites) with a wall-clock backstop (`MAIN2MAIN_EXTENDED_MAX_MIN`, default
360) and a stop-loss on an identical failing set; pytest exit 4
(collection error) suites are excluded from fix rounds permanently.  Each
fix round re-checks the gate statics when the tree changed (statics fail →
one adapter round → still failing ends the phase but KEEPS the fixes).
The phase is BEST-EFFORT: exhausting every budget never fails the run —
fixes are committed (`main2main: extended e2e fixes (round N)`), `gate_final_patch`
is regenerated so the PR description carries them, and upstream PR CI
remains the verifier.  Evidence: `workspace/extended_e2e/extended_e2e_result.json`
+ an "Extended e2e" section in `final_summary.md`/the PR body.
`MAIN2MAIN_EXTENDED_E2E=0` disables the phase.

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
| `MAIN2MAIN_EXTENDED_E2E` | `0` disables the post-gate extended e2e phase (default: `1`). |
| `MAIN2MAIN_EXTENDED_FIX_ROUNDS` | Adapter-fix rounds for extended e2e failures after the first full round (default: `2`). |
| `MAIN2MAIN_EXTENDED_MAX_MIN` | Extended e2e wall-clock backstop in minutes, `0` = off (default: `360`). |
| `MAIN2MAIN_EXTENDED_TEST_CASES` | Space/newline-separated extended e2e cases replacing the resolver output entirely. |
| `MAIN2MAIN_EXTENDED_INCLUDE_SKIPPED` | `1` keeps upstream `skip_tests` files in the extended set (default: `0`). |
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
