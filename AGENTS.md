# AGENTS.md

CrewAI Flow that automates vllm-ascend's main2main upgrade against upstream vLLM. Drives an external `opencode run` subprocess as the AI adapter; everything else is deterministic Python.

## Run

Install once: `pip install -e .`

Real entrypoint is `Main2MainFlow` in `main2main_flow/flow.py`. Install and run:

```bash
pip install -e .
kickoff --vllm-path <path|url> --vllm-ascend-path <path|url> [--target-commit SHA]
```

Both repos must be real git checkouts (or HTTPS URLs that will be cloned into `workspace/repos/`). vllm HEAD is the implicit target unless `--target-commit` is given.

## Layout

- `main2main_flow/flow.py` — the Flow; node order: `initialize → analyze_commit_and_plan_step → process_steps → generate_final_post → push_to_github`. Routing uses string signals defined in `scripts/utils/utils.py` (`HasCommit`, `HasNoCommit`, `UpgradeCompleted`, `UpgradeFailed`).
- `main2main_flow/cli.py` — CLI entry point (`kickoff`, `plot`, `run_with_trigger`).
- `main2main_flow/agents/` — agent SKILL.md files and per-role reference docs consumed by opencode. Each role is a self-contained directory:
  - `adapter/SKILL.md` + `adapter/reference/` — adapt and fix modes
  - `adapter-qa/SKILL.md` + `adapter-qa/reference/` — independent reviewer
- `main2main_flow/scripts/agent/opencode_adapter.py` — spawns `opencode run --format json --dangerously-skip-permissions`, streams JSONL, 30 min total / 5 min stale timeouts, supports `--session` for persistent sessions.
- `main2main_flow/scripts/utils/` — deterministic helpers and shared utilities:
  - `utils.py` — filename constants, git helpers, `ts_print`
  - `detect_commits.py`, `plan_steps.py`, `update_commit_reference.py` — commit detection and planning
  - `pre_ci_check.py` — version strings, temp files, format, broken imports checks
  - `run_tests.py` — e2e test runner with parallel scheduling
  - `push_to_github.py` — push branch + create PR + add labels
  - `ci_log_summary.py` — test log parsing

## workspace/ is volatile

`initialize` **deletes and recreates** `workspace/` on every run. Never put anything there you want to keep. All step artifacts (`workspace/steps/<step-id>/upstream.patch`, `step_summary.md`, `step_target.patch`, `opencode.log`, `opencode_raw.jsonl`, `tests/round-*-result.json`, `pre_ci_check.json`) live under it. Filenames are centralised as constants in `scripts/utils/utils.py` — reuse them, don't hardcode strings.

## State & path constants

- `WORKSPACE_DIR = <repo>/workspace` (computed from `__file__`, respects `MAIN2MAIN_WORKSPACE` env var).
- Path resolution priority in `initialize`: CLI arg → env var (`VLLM_PATH`, `VLLM_ASCEND_PATH`, `VLLM_TARGET_COMMIT`) → default. URLs starting with `http(s)://` or `git@` are auto-cloned; existing target dirs get **removed first**.
- `initialize` records `original_vllm_ref` / `original_ascend_ref` and `generate_final_post` checks them back out (`-f` for ascend). If you add new checkouts/branch switches mid-flow, make sure restoration still works.

## Retry & test loop semantics

`process_steps`: per step, run `_ai_analysis` then `_run_e2e_test`. Pass → next step, reset `retry_count`. Fail → `retry_count++` and re-enter `_ai_analysis` in fix mode. At `retry_count >= 3` the entire flow short-circuits to `UpgradeFailed` — there is no per-step skip.

Inside `_ai_analysis`, the attempt loop (up to 3×):
1. **adapter** (role=adapter) — generates adaptations
2. `format.sh` — lint + format
3. `run_check` — pre-CI: version_strings, temp_files, format, broken_imports
4. **adapter-qa** — independent AI review (separate opencode session, no generator context)
5. All pass → break. Any fail → retry with **adapter-fix** (role=adapter-fix, with error_logs inlined).

## Env flags worth knowing

| Var | Effect |
|---|---|
| `SKIP_AI_ANALYSIS=true` | Bypass opencode entirely; only deterministic ops run. Useful for debugging the Flow plumbing. |
| `SKIP_E2E_TEST=true` | `_run_e2e_test` returns True without touching NPU. |
| `PUSH_TO_GITHUB=true` + `GITHUB_REPO=owner/name` | Enables `push_to_github`; requires `gh` logged in. |
| `HEAD_FORK=org/name` | Fork repo to push to (default: `vllm-ascend-ci/vllm-ascend`). |
| `MAIN2MAIN_MODEL=provider/model` | opencode model (default: `deepseek/deepseek-chat`). Per-role overrides: `MAIN2MAIN_MODEL_ADAPT`, `MAIN2MAIN_MODEL_FIX`, `MAIN2MAIN_MODEL_REVIEW`. |
| `MAIN2MAIN_TIMEOUT_MIN` | opencode total timeout minutes (default: 30). |
| `MAIN2MAIN_STALE_SEC` | opencode stale timeout seconds (default: 300). |
| `MAIN2MAIN_WORKSPACE` | Workspace root directory (default: `<repo>/workspace`). |
| `MAIN2MAIN_TEST_CASES` | Space-separated test paths to run. |
| `MAIN2MAIN_KEEP_BRANCH` | Skip `git reset --hard origin/main` in vllm-ascend setup. |
| `PR_LABELS` | Comma-separated labels for created PR (default: `ready`). |
| `PR_DRAFT` | Create draft PR (default: `true`). |
| `MAIN2MAIN_RUN_TESTS_REMOTE` | Run tests on a remote host via SSH (`user@host` or `env`). |
| `MAIN2MAIN_REMOTE_HOST`, `MAIN2MAIN_REMOTE_CONTAINER` | SSH host and container for remote e2e tests. |

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
