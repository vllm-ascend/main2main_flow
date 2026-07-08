# Upstream Main2Main Upgrade Flow

Automate vllm-ascend's [main2main upgrade](docs/guide.md) against upstream vLLM.

Each time vLLM's `main` advances, vllm-ascend must catch up: bump the recorded
upstream commit, adapt any broken interfaces, and re-run e2e CI. This project
drives that whole loop:

- detect the commit gap, plan it into bite-sized steps
- for every step, run an `opencode` AI agent to adapt the code, gate it with a
  deterministic pre-CI check, then have an independent AI critic review the diff
- run real NPU e2e tests, retry on failure (up to 3 rounds per step)
- when everything passes, push a branch and open (or update) a PR

Full walkthrough lives in [`docs/guide.md`](docs/guide.md); this README only
covers how to install and run.

![Flow diagram](docs/images/flow.png)

## Requirements

- Python 3.10–3.13
- [`opencode`](https://opencode.ai) CLI on `$PATH` (used as the AI adapter),
  plus `MAIN2MAIN_MODEL` set to an opencode `provider/model` id — the adapter
  refuses to run without it (not needed with `SKIP_AI_ANALYSIS=true`)
- `git`, plus local checkouts of `vllm` and `vllm-ascend` (or HTTPS URLs to
  clone)
- For real e2e tests: a host with Ascend NPUs reachable over SSH, with a
  prepared Docker container
- For automated PRs: [`gh`](https://cli.github.com/) logged in

## Install

```bash
pip install -e .
```

(`uv sync` also works if you use [`uv`](https://docs.astral.sh/uv/). Note that
`uv.lock` is gitignored, so a fresh clone resolves dependencies from
`pyproject.toml` rather than a pinned lock.)

## Run

```bash
MAIN2MAIN_MODEL=anthropic/claude-sonnet-4-6 \
kickoff \
  --vllm-path        /path/to/vllm \
  --vllm-ascend-path /path/to/vllm-ascend \
  [--target-commit   <40-char SHA>]
```

- Both paths may be local git checkouts **or** HTTPS / SSH git URLs — URLs are
  auto-cloned into `repo_cache/` at the repo root. Cache clones persist across
  runs and are hard-synced to `origin` on reuse (never point `repo_cache/` at a
  checkout you care about).
- Both working trees must be clean; a dirty tree aborts the run unless
  `MAIN2MAIN_ALLOW_DIRTY=true`.
- `--target-commit` is optional; defaults to vllm `HEAD` (see also
  `MAIN2MAIN_TARGET_REF`).
- Each run wipes and recreates `workspace/`, so back it up if you need the
  artifacts from a previous run.
- Only one run at a time: a lock file (`.main2main.lock`) guards against
  overlapping runs; a second `kickoff` exits with code 2.

CLI flags can also be supplied via env vars: `VLLM_PATH`, `VLLM_ASCEND_PATH`,
`VLLM_TARGET_COMMIT` (CLI wins).

### Common variations

```bash
# Clone both repos from GitHub, target vllm HEAD
kickoff \
  --vllm-path        https://github.com/vllm-project/vllm.git \
  --vllm-ascend-path https://github.com/vllm-project/vllm-ascend.git

# Dry-run plumbing: skip both opencode and NPU tests (no MAIN2MAIN_MODEL needed)
SKIP_AI_ANALYSIS=true SKIP_E2E_TEST=true kickoff \
  --vllm-path /path/to/vllm --vllm-ascend-path /path/to/vllm-ascend

# Run e2e tests on a remote NPU box via SSH + docker exec.
# Setting both vars is enough: MAIN2MAIN_RUN_TESTS_REMOTE defaults to "env"
# when host + container are set, so it does not need to be exported explicitly.
MAIN2MAIN_REMOTE_HOST=root@10.0.0.10 \
MAIN2MAIN_REMOTE_CONTAINER=vllm-ascend-ci \
kickoff --vllm-path ... --vllm-ascend-path ...

# Auto-push a branch and open a PR after a successful run
PUSH_TO_GITHUB=true GITHUB_REPO=vllm-project/vllm-ascend \
kickoff --vllm-path ... --vllm-ascend-path ...
```

### Environment variables

Repos and target:

| Variable | Purpose | Default |
|---|---|---|
| `VLLM_PATH` | vllm repo (path or URL; URLs clone into `repo_cache/`) | — |
| `VLLM_ASCEND_PATH` | vllm-ascend repo (path or URL) | — |
| `VLLM_TARGET_COMMIT` | target vllm commit SHA | vllm `HEAD` |
| `MAIN2MAIN_TARGET_REF` | e.g. `origin/main`; when set and no target commit is given, fetches vllm and resolves this ref as the target | unset |
| `MAIN2MAIN_WORKSPACE` | override the workspace directory | `<repo>/workspace` |

AI adapter (opencode):

| Variable | Purpose | Default |
|---|---|---|
| `MAIN2MAIN_MODEL` | opencode `provider/model` id, e.g. `deepseek/deepseek-v4-pro`, `zhipuai/glm-5.1`, `anthropic/claude-sonnet-4-6`. **Required whenever the AI agent runs** — the adapter raises a clear error if unset | — |
| `MAIN2MAIN_MODEL_ADAPT` / `MAIN2MAIN_MODEL_FIX` / `MAIN2MAIN_MODEL_REVIEW` | per-mode model overrides (`fix_preci` uses `_FIX`); fall back to `MAIN2MAIN_MODEL` | unset |
| `MAIN2MAIN_TIMEOUT_MIN` | opencode total timeout (minutes) | `30` |
| `MAIN2MAIN_STALE_SEC` | opencode stale-output timeout (seconds); a stalled run is killed and resumed via its session id | `300` |
| `MAIN2MAIN_REVIEW_TIMEOUT_MIN` | critic total timeout (minutes) | `10` |
| `MAIN2MAIN_CRITIC` | run the independent critic review after pre-CI passes | `true` |
| `SKIP_AI_ANALYSIS` | skip the opencode agent, only run deterministic steps | `false` |

Testing:

| Variable | Purpose | Default |
|---|---|---|
| `SKIP_E2E_TEST` | skip the NPU e2e tests, treat as passed | `false` |
| `MAIN2MAIN_REMOTE_HOST` | SSH host running the NPU container | — |
| `MAIN2MAIN_REMOTE_CONTAINER` | Docker container name on that host | — |
| `MAIN2MAIN_RUN_TESTS_REMOTE` | force remote test execution; auto-defaults to `env` when both host + container vars are set | auto |
| `MAIN2MAIN_TEST_CASES` | space-separated test paths that override automatic test selection | unset |
| `MAIN2MAIN_SMOKE_TESTS` | space-separated test paths run when test selection yields nothing (otherwise the step passes without runtime validation, with a loud warning) | unset |
| `MAIN2MAIN_SSH_STRICT` | value for ssh `StrictHostKeyChecking` | `accept-new` |
| `MAIN2MAIN_USE_CN_MIRRORS` | enable Tsinghua/huaweicloud pip mirror setup (local and remote) | `false` |
| `SKIP_PIP_INSTALL` | skip pip installs during test env setup | `false` |
| `ASCEND_RT_VISIBLE_DEVICES` | restrict which NPU card IDs are used | auto-detected |

Safety and limits:

| Variable | Purpose | Default |
|---|---|---|
| `MAIN2MAIN_ALLOW_DIRTY` | warn instead of fail when a repo tree starts dirty | `false` |
| `MAIN2MAIN_MAX_HOURS` | global wall-clock budget in hours; `0` disables | `12` |
| `MAIN2MAIN_DEBUG` | print the full traceback when the flow fails | `false` |

Planning:

| Variable | Purpose | Default |
|---|---|---|
| `MAIN2MAIN_PLAN_EXCLUDES` | space-separated git pathspecs excluded from step planning and patches (e.g. `vllm/attention/ops/triton_*`) | unset |
| `MAIN2MAIN_REF_UPDATE_PATHS` | comma-separated fnmatch globs restricting which tracked files get the commit-ref replacement | unset (all tracked files) |

Push / PR:

| Variable | Purpose | Default |
|---|---|---|
| `PUSH_TO_GITHUB` | open a PR after the run | `false` |
| `GITHUB_REPO` | PR target, `owner/name` | — |
| `HEAD_FORK` | fork to push the branch to, `owner/name` | unset |
| `PR_DRAFT` | open the PR as a draft | `true` |
| `PR_LABELS` | comma-separated labels | `ready` |
| `PR_BRANCH_NAME` | explicit branch name (overrides dedup/timestamped naming) | unset |
| `MAIN2MAIN_PR_DEDUP` | reuse the fixed `main2main/auto-sync` branch (force-with-lease) and update the existing open PR instead of creating new ones each run | `true` |
| `MAIN2MAIN_KEEP_BRANCH` | commit on the current branch and skip repo restoration; pushing is refused when the run failed | `false` |
| `GH_TOKEN` | GitHub token for push/`gh` (stripped from the opencode subprocess env) | — |

## Outputs

Per-run artifacts land under `workspace/` (recreated on every run):

```
workspace/
├── detect.json                # base / target commits, compat tag
├── steps.json                 # step plan (metadata only, no patch bodies)
├── final_summary.md           # PR body: per-file changes + upstream commit links
├── final_target.patch         # cumulative vllm-ascend diff
├── final_status.json          # status, steps completed, reached commit, failure_reason, model
└── steps/<step-id>/
    ├── upstream.patch         # this step's vllm diff
    ├── changed_files.txt
    ├── analysis.md            # agent-written analysis
    ├── review.md              # agent-written self-review
    ├── step_summary.md        # cumulative AI-written summary
    ├── result.json            # agent completion contract: {"status": "adapted"|"noop", ...}
    ├── review.json            # critic verdict (when the critic ran)
    ├── step_target.patch      # cumulative vllm-ascend diff for this step
    ├── pre_ci_check.json      # written when a pre-CI attempt fails
    ├── opencode-r0-a1.log     # per-attempt logs: r<retry-round>-a<opencode-attempt>
    ├── opencode-r0-a1_raw.jsonl
    ├── opencode-r0-a1_stderr.log
    ├── opencode-review-r0-a1.log      # critic pass logs
    └── tests/
        ├── round-<n>-<test-slug>.log
        ├── round-<n>-<test-slug>-summary.json
        └── round-<n>-result.json      # aggregate e2e result for retry round n
```

A few things persist at the repo root across runs:

- `repo_cache/` — clones of repos passed as URLs (hard-synced to origin on reuse)
- `runs-ledger.jsonl` — one JSON line per run (base, target, status, steps, model)
- `.main2main.lock` — single-run lock file
- `main2main_flow/reference/candidates.md` — error→fix pairs appended
  automatically after a failed→fixed round, for curation into the knowledge base

## Project layout

```
main.py                               # convenience entry point
main2main_flow/
├── cli.py                            # CLI (kickoff, plot, etc.)
├── flow.py                           # Flow: nodes, routing, retry loop, critic
├── utils.py                          # filename constants, git helpers, FlowLock
├── agent/
│   ├── opencode_adapter.py           # spawns `opencode run`, parses JSONL events
│   ├── prompt.md                     # adapt / fix task prompt
│   ├── prompt_fix_preci.md           # minimal-fix prompt for pre-CI failures
│   └── review_prompt.md              # independent critic prompt
├── reference/                        # knowledge base the agent reads at runtime
│   ├── adapt-guide.md
│   ├── ascend-constraints.md
│   ├── candidates.md                 # auto-appended fix candidates (staging)
│   ├── code-structure-guide.md
│   ├── diagnosis-guide.md
│   ├── error-pattern-examples.md
│   ├── output-exemplars.md
│   ├── review-lessons.md             # §9 is the critic's checklist
│   ├── upstream-ignore-paths.md
│   └── versioning-primer.md
└── scripts/                          # deterministic helpers (no AI)
    ├── detect_commits.py
    ├── plan_steps.py
    ├── update_commit_reference.py
    ├── pre_ci_check.py
    ├── run_tests.py
    ├── ci_log_summary.py
    ├── push_to_github.py
    └── replay_eval.py                # replay harness: score runs against known-good cases
```

`replay_eval.py` replays historical commit ranges through the flow and scores
the produced patch against expected files (precision/recall) — useful before
switching models or prompts:

```bash
python -m main2main_flow.scripts.replay_eval \
  --vllm-path /path/to/vllm --ascend-path /path/to/vllm-ascend \
  --cases cases.json [--workdir replay_workdir]
```

For a step-by-step explanation of every node and the per-step artifacts, see
[`docs/guide.md`](docs/guide.md). For conventions and gotchas that affect code
changes to this repo itself, see [`AGENTS.md`](AGENTS.md).
