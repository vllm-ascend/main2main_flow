# Upstream Main2Main Upgrade Flow

Automate vllm-ascend's [main2main upgrade](docs/guide.md) against upstream vLLM.

Each time vLLM's `main` advances, vllm-ascend must catch up: bump the recorded
upstream commit, adapt any broken interfaces, and re-run e2e CI. This project
drives that whole loop:

- detect the commit gap, plan it into bite-sized steps (commit impact routed
  via the vllm-report knowledge base over MCP)
- for every step, run an `opencode` AI agent to adapt the code, then a
  deterministic pre-CI check and an independent critic review
- run real NPU e2e tests, retry on failure (up to 3×); no-op steps skip e2e
- a push-time quality gate re-runs format + mypy + CPU-UT on the final diff
- lessons from fix rounds are persisted back to vllm-report for future runs
- then push a branch and open a PR (label `ready-all` triggers PR CI, whose
  failures are tracked back into vllm-report lessons — the full feedback loop
  is in `docs/guide.md`)

Full walkthrough lives in [`docs/guide.md`](docs/guide.md); this README only
covers how to install and run.

![Flow diagram](docs/images/workflow.png)

## Requirements

- Python 3.10–3.13
- [`opencode`](https://opencode.ai) CLI on `$PATH` (used as the AI adapter)
- `git`, plus local checkouts of `vllm` and `vllm-ascend` (or HTTPS URLs to
  clone)
- For real e2e tests: a host with Ascend NPUs reachable over SSH, with a
  prepared Docker container
- For automated PRs: [`gh`](https://cli.github.com/) logged in

## Install

```bash
pip install -e .
```

(`uv sync` also works if you use [`uv`](https://docs.astral.sh/uv/); the repo
already ships a `uv.lock`.)

## Run

```bash
kickoff \
  --vllm-path        /path/to/vllm \
  --vllm-ascend-path /path/to/vllm-ascend \
  [--target-commit   <40-char SHA>]
```

- Both paths may be local git checkouts **or** HTTPS / SSH git URLs — URLs are
  auto-cloned into `workspace/repos/`.
- `--target-commit` is optional; defaults to vllm `HEAD`.
- Each run wipes and recreates `workspace/`, so back it up if you need the
  artifacts from a previous run.

CLI flags can also be supplied via env vars: `VLLM_PATH`, `VLLM_ASCEND_PATH`,
`VLLM_TARGET_COMMIT` (CLI wins).

### Common variations

```bash
# Clone both repos from GitHub, target vllm HEAD
kickoff \
  --vllm-path        https://github.com/vllm-project/vllm.git \
  --vllm-ascend-path https://github.com/vllm-project/vllm-ascend.git

# Dry-run plumbing: skip both opencode and NPU tests
SKIP_AI_ANALYSIS=true SKIP_E2E_TEST=true kickoff \
  --vllm-path /path/to/vllm --vllm-ascend-path /path/to/vllm-ascend

# Run e2e tests on a remote NPU box via SSH + docker exec
MAIN2MAIN_REMOTE_HOST=root@10.0.0.10 \
MAIN2MAIN_REMOTE_CONTAINER=vllm-ascend-ci \
kickoff --vllm-path ... --vllm-ascend-path ...

# Auto-push a branch and open a PR after a successful run
PUSH_TO_GITHUB=true GITHUB_REPO=vllm-project/vllm-ascend \
kickoff --vllm-path ... --vllm-ascend-path ...
```

### Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `VLLM_PATH` | vllm repo (path or URL) | `workspace/repos/vllm` |
| `VLLM_ASCEND_PATH` | vllm-ascend repo (path or URL) | `workspace/repos/vllm-ascend` |
| `VLLM_TARGET_COMMIT` | target vllm commit SHA | vllm `HEAD` |
| `SKIP_AI_ANALYSIS` | skip the opencode agent, only run deterministic steps | `false` |
| `SKIP_E2E_TEST` | skip the NPU e2e tests, treat as passed | `false` |
| `PUSH_TO_GITHUB` | open a PR after success | `false` |
| `GITHUB_REPO` | PR target, `owner/name` | — |
| `PR_REPO` | PR target override, `owner/name` (GITHUB_REPO stays the issue/chain target) | `GITHUB_REPO` |
| `MAIN2MAIN_BASELINE_REF` | baseline ref on HEAD_FORK updated after a successful PR | `main2main_baseline` |
| `PR_LABELS` | labels for the created PR | `ready-all` |
| `MAIN2MAIN_MODEL` | opencode model (per-role: `_ADAPT`/`_FIX`/`_REVIEW`) | `deepseek/deepseek-chat` |
| `MAIN2MAIN_TIMEOUT_MIN` / `MAIN2MAIN_STALE_SEC` | opencode total / idle timeouts | `30` / `300` |
| `MAIN2MAIN_KEEP_BRANCH` | reuse the existing branch instead of resetting to `origin/main` | `false` |
| `MAIN2MAIN_UT_SKIP_A2` | CPU-UT only, skip the A2 NPU UT batch | `false` |
| `MAIN2MAIN_REMOTE_HOST` | SSH host running the NPU container | — |
| `MAIN2MAIN_REMOTE_CONTAINER` | Docker container name on that host | — |

## Outputs

Everything lands under `workspace/` (recreated on every run):

```
workspace/
├── detect.json            # base / target commits, compat tag
├── steps.json             # full step plan
├── final_summary.md       # PR body (Changes table)
├── final_target.patch     # accumulated vllm-ascend diff (post-gate)
├── final_status.json      # status / steps_completed / old & new commit
├── gate_final_patch       # gate-regenerated accumulated patch
├── repos/vllm-report/     # knowledge base clone (MCP server)
├── quality_gate/          # final quality gate artifacts
└── steps/<step-id>/
    ├── upstream.patch     # this step's vllm diff
    ├── changed_files.txt
    ├── analysis.md        # adapter's analysis / fix notes
    ├── result.json        # adapter's structured result (adapted / noop)
    ├── step_target.patch  # vllm-ascend diff for this step
    ├── step_summary.md    # AI-written summary
    ├── pre_ci_check.json  # deterministic pre-CI result
    ├── review.json        # critic verdict
    ├── opencode.log       # opencode conversation log
    ├── opencode_raw.jsonl # raw event stream
    └── tests/
        ├── round-<n>-<suite>.log
        ├── round-<n>-<suite>-summary.json
        ├── round-<n>-result.json
        └── round-<n>-test-errors.txt
```

## Project layout

```
main.py                               # convenience entry point
main2main_flow/
├── cli.py                            # CLI (kickoff)
├── flow.py                           # Flow: nodes, routing, retry loop, quality gate
├── agents/                           # opencode agent SKILL.md + per-role reference
│   ├── adapter/
│   │   ├── SKILL.md                  #   adapt + fix prompt
│   │   └── reference/                #   adaptation-patterns, common-pitfalls, code-structure
│   ├── adapter-qa/
│   │   ├── SKILL.md                  #   independent reviewer prompt
│   │   └── reference/                #   review-lessons.md
│   └── description-fill/
│       └── SKILL.md                  #   PR-description file attribution analysis
└── scripts/
    ├── agent/
    │   └── opencode_adapter.py       # spawns `opencode run`, parses JSONL events
    └── utils/                        # deterministic helpers + shared utilities
        ├── utils.py                  #   filename constants, git helpers, ts_print
        ├── detect_commits.py
        ├── plan_steps.py
        ├── commit_ref.py             #   verified-commit reference replacement
        ├── pre_ci_check.py           #   per-step checks (version/temp/imports/format)
        ├── final_quality_gate.py     #   push-time gate: format + mypy + UT
        ├── ut_check.py               #   CPU-UT runner (per-file isolation)
        ├── run_tests.py
        ├── ci_log_summary.py
        ├── lessons.py                #   lesson submit/persist to vllm-report
        ├── track_pr_ci.py            #   PR CI result tracking (vllm-report step 10)
        └── push_to_github.py
```

For a step-by-step explanation of every node and the per-step artifacts, see
[`docs/guide.md`](docs/guide.md). For conventions and gotchas that affect code
changes to this repo itself, see [`AGENTS.md`](AGENTS.md).
