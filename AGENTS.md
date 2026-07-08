# AGENTS.md

CrewAI Flow that automates vllm-ascend's main2main upgrade against upstream vLLM. Drives an external `opencode run` subprocess as the AI adapter; everything else is deterministic Python.

## Run

Install once: `pip install -e .`

Real entrypoint is `Main2MainFlow` in `main2main_flow/flow.py`. Install and run:

```bash
pip install -e .
MAIN2MAIN_MODEL=<provider/model> kickoff --vllm-path <path|url> --vllm-ascend-path <path|url> [--target-commit SHA]
```

Both repos must be real git checkouts (or HTTPS URLs that will be cloned into `repo_cache/` at the repo root). Both trees must be clean at start (`MAIN2MAIN_ALLOW_DIRTY=true` downgrades that to a warning). vllm HEAD is the implicit target unless `--target-commit` or `MAIN2MAIN_TARGET_REF` is given. `kickoff` takes a non-blocking flock on `.main2main.lock` — a concurrent run exits with code 2.

## Layout (only the non-obvious bits)

- `main2main_flow/flow.py` — the Flow; node order: `initialize → analyze_commit_and_plan_step → process_steps → generate_final_post → push_to_github`. The only routing signals are `HasCommit` / `HasNoCommit` (returned by the `@router`). `UpgradeCompleted` / `UpgradeFailed` are **not** routing signals — they are the values stored in `state.final_status`. All four constants live in `utils.py`; match them exactly.
- `main2main_flow/scripts/` — deterministic helpers (`detect_commits`, `plan_steps`, `update_commit_reference`, `pre_ci_check`, `run_tests`, `ci_log_summary`, `push_to_github`, `replay_eval`). Import them, don't shell out. Library paths raise `RuntimeError` on failure; only CLI `main()` functions call `sys.exit`.
- `main2main_flow/agent/opencode_adapter.py` — spawns `opencode run --format json --dangerously-skip-permissions`. Model comes from `MAIN2MAIN_MODEL` (per-mode overrides `MAIN2MAIN_MODEL_ADAPT/_FIX/_REVIEW`); unset → `RuntimeError`, checked lazily at call time (importing the module needs neither opencode nor the env var). Timeouts: `MAIN2MAIN_TIMEOUT_MIN` (30) total / `MAIN2MAIN_STALE_SEC` (300) stale, up to 3 stale retries; when a session id was captured from the event stream the retry resumes via `opencode run --session <id>` with a short continue prompt, otherwise it re-sends the full task. `GH_TOKEN`/`GITHUB_TOKEN` are stripped from the subprocess env. `run_opencode_review` is the single-attempt critic entry point.
- `main2main_flow/agent/*.md` — three prompt templates: `prompt.md` (adapt/fix), `prompt_fix_preci.md` (self-contained minimal-fix for pre-CI failures), `review_prompt.md` (critic). All are formatted with `str.format_map`, so any literal `{}` must be escaped as `{{ }}`, and every `{var}` must exist in the inputs dict built in `flow.py` or `format_map` will KeyError at runtime.
- `main2main_flow/reference/` — knowledge base consumed by the agent: `versioning-primer.md`, `adapt-guide.md`, `diagnosis-guide.md`, `error-pattern-examples.md`, `review-lessons.md` (§9 is the critic checklist fed via `_review_checklist()`), `output-exemplars.md`, `ascend-constraints.md`, `upstream-ignore-paths.md`, `code-structure-guide.md` (the flow parses its `file-mapping` marker block for the stale-mapping check), and `candidates.md` (auto-appended by `_record_fix_candidate` after a failed→fixed round; curate entries into `error-pattern-examples.md` and delete them).
- `docs/guide.md` — long-form walkthrough (Chinese). When in doubt about behavior, trust `flow.py` + `utils.py`.

## workspace/ is volatile, repo_cache/ is persistent

`initialize` **deletes and recreates** `workspace/` on every run (`MAIN2MAIN_WORKSPACE` can relocate it). Never put anything there you want to keep. All step artifacts (`workspace/steps/<step-id>/upstream.patch`, `analysis.md`, `review.md`, `step_summary.md`, `result.json`, `review.json`, `step_target.patch`, `pre_ci_check.json`, `opencode-r<round>-a<attempt>{.log,_raw.jsonl,_stderr.log}`, `tests/round-*-result.json`) live under it.

Persistent at the repo root (all gitignored except `reference/candidates.md`): `repo_cache/` (URL clones, hard-synced to origin on reuse — flow-owned, never a user checkout), `runs-ledger.jsonl` (one JSON line appended per run by `generate_final_post`), `.main2main.lock`.

Filenames are centralised as constants in `utils.py` — reuse them, don't hardcode strings.

## State & path constants

- `WORKSPACE_DIR = <repo>/workspace` unless `MAIN2MAIN_WORKSPACE` overrides it; `PROJECT_ROOT` is computed from `__file__`, not cwd.
- Path resolution priority in `initialize`: CLI arg → env var (`VLLM_PATH`, `VLLM_ASCEND_PATH`, `VLLM_TARGET_COMMIT`) → default. URLs starting with `http(s)://` or `git@` are cloned into `repo_cache/<name>`; an existing cache clone is fetched + `reset --hard origin/HEAD`; a non-git directory at that path raises (never rmtree'd).
- `initialize` records `original_vllm_ref` / `original_ascend_ref` and `generate_final_post` checks them back out (`git reset` + `checkout -f` for ascend to also clear intent-to-add entries), wrapped in try/except so restoration failures never mask the run result. If you add new checkouts/branch switches mid-flow, make sure restoration still works. With `MAIN2MAIN_KEEP_BRANCH=true` restoration is skipped entirely.
- Key state fields beyond the obvious: `last_verified_vllm_commit` (updated only on e2e pass; used for partial-progress reporting), `failure_reason`, `last_error_excerpt`, `prev_step_files` (cumulative changed-file list after the previous verified step; the delta feeds `candidates.md`).

## Retry & test loop semantics

`process_steps`: per step, run `_ai_analysis` (returns bool) then `_run_e2e_test`. Pass → next step, reset `retry_count`, update `last_verified_vllm_commit`; if the pass came after retries, `_record_fix_candidate` appends the error→fix pair to `reference/candidates.md`. Fail → `retry_count++` and re-enter `_ai_analysis`. At `retry_count >= 3` the entire flow sets `final_status = UpgradeFailed` — there is no per-step skip. A global wall-clock budget (`MAIN2MAIN_MAX_HOURS`, default 12h) is checked before each step. Exceptions never crash the flow: `StepFailure` carries the reason; anything else is logged and treated as a failed round, so `generate_final_post` (and repo restoration) always runs.

Inside `_ai_analysis`, opencode is called up to 3 times per round:

- Mode selection: attempt 1 runs `fix` when e2e errors exist, else `adapt`; attempts ≥2 run `fix` when e2e errors or a critic rejection are in play, else `fix_preci` (a minimal-change prompt fed the inlined pre-CI failure).
- Liveness: an adapt/fix attempt that produced no `analysis.md`, or an opencode process that died / timed out / emitted no events (`AdaptResult.agent_failed`), counts as a failed attempt.
- Pre-CI (`pre_ci_check.run_check`: version strings, temp files, format.sh, AST-based broken-imports on added lines) gates every attempt; a failure writes `pre_ci_check.json` and feeds the next attempt. **Exhausting the 3 attempts raises `StepFailure` and fails the round** (it no longer proceeds to e2e).
- After pre-CI passes, the critic runs (unless `MAIN2MAIN_CRITIC=false`): an independent opencode invocation reviews the cumulative diff against the §9 checklist and writes `review.json`. Verdict `fail` → issues are fed to the next attempt in `fix` mode; a crashed critic is logged and the adaptation is accepted.
- Error context is inlined into prompts as `error_content` (JSON from the error-log files + critic issues, capped at 8000 chars), not just file paths.

`_run_e2e_test` calls `run_tests(..., in_place=True)` for local runs (the flow's live checkouts already carry the adaptation — no fetch/reset/patch), or remote mode when `MAIN2MAIN_RUN_TESTS_REMOTE` is set / auto-derived from `MAIN2MAIN_REMOTE_HOST` + `_CONTAINER`. Test selection: `MAIN2MAIN_TEST_CASES` override → select_tests.py on changed files → `MAIN2MAIN_SMOKE_TESTS` floor → skipped (`tests_skipped=True`, loud warning, step passes without runtime validation). The result dict always carries `result_path` (the written `round-<n>-result.json`) and `tests_skipped`; on failure the flow stores `result_path` in `state.test_errors` for the next fix round.

## Env flags worth knowing

| Var | Effect |
|---|---|
| `MAIN2MAIN_MODEL` (+ `_ADAPT`/`_FIX`/`_REVIEW`) | Required for AI runs; opencode `provider/model`. Per-mode overrides fall back to the base var. |
| `SKIP_AI_ANALYSIS=true` | Bypass opencode entirely; only deterministic ops run. Useful for debugging the Flow plumbing. |
| `SKIP_E2E_TEST=true` | `_run_e2e_test` returns True without touching NPU. |
| `MAIN2MAIN_CRITIC=false` | Skip the critic review pass. |
| `MAIN2MAIN_ALLOW_DIRTY=true` | Start despite dirty repo trees (loud warning instead of abort). |
| `MAIN2MAIN_MAX_HOURS` | Wall-clock budget (default 12; 0 disables). |
| `MAIN2MAIN_TARGET_REF` | Resolve e.g. `origin/main` as the target after a fetch, instead of a pinned SHA. |
| `MAIN2MAIN_PLAN_EXCLUDES` | git pathspecs excluded from planning/patches. |
| `MAIN2MAIN_REF_UPDATE_PATHS` | fnmatch globs restricting commit-ref replacement. |
| `PUSH_TO_GITHUB=true` + `GITHUB_REPO=owner/name` | Enables `push_to_github`; requires `gh` logged in. `MAIN2MAIN_PR_DEDUP` (default true) reuses the `main2main/auto-sync` branch and updates the existing open PR. |
| `MAIN2MAIN_REMOTE_HOST`, `MAIN2MAIN_REMOTE_CONTAINER` | ssh+docker-exec test target; setting both auto-enables remote mode. Mac dev boxes have no NPU — always set these or use `SKIP_E2E_TEST=true`. |
| `MAIN2MAIN_SMOKE_TESTS` | Floor test set when selection yields nothing. |
| `MAIN2MAIN_DEBUG=true` | Full traceback from `kickoff` on failure. |

## Conventions

- Python 3.10–3.13. Uses `uv`; `uv.lock` is gitignored (not committed). The `.venv/` at repo root is the uv venv — don't recreate.
- No lint/typecheck/test commands are wired up. `tests/` is empty. Don't invent a `pytest` invocation; verify changes by `SKIP_E2E_TEST=true SKIP_AI_ANALYSIS=true kickoff ...` against a small synthetic commit range.
- All adapter outputs that need persistence go through `utils.py` filename constants; introducing a new artifact means adding a constant there first.
- vllm-ascend version guards must use `vllm_version_is("{release_tag}")` exactly — `pre_ci_check` will reject any new `vllm_version_is(...)` call whose tag doesn't match `state.release_tag`.

## Don'ts

- Forget to `pip install -e .` after restructuring the code.
- Don't keep `workspace/` paths between runs; they vanish on `initialize`. (`repo_cache/`, `runs-ledger.jsonl` and `reference/candidates.md` are the deliberate exceptions.)
- Don't add `{var}` placeholders to `prompt.md`, `prompt_fix_preci.md`, or `review_prompt.md` that aren't passed into the corresponding inputs dict in `flow.py`, or `format_map` will KeyError.
- Don't commit anything under `workspace/`, `repo_cache/`, `output/`, `.venv/`, or `.env` (already gitignored).
