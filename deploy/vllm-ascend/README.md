# schedule_main2main.yaml — a3-16 pool deployment draft

Ready-to-apply replacement for vllm-project/vllm-ascend
`.github/workflows/schedule_main2main.yaml`. Built from upstream `deacd4201`
structure + the pool deltas proven on the vllm-benchmarks `m2m-a3-16`
validation branch (runs 33883282362 → 34110449889).

## Deltas vs upstream

| Area | Change |
|---|---|
| runner | main2main job: `linux-aarch64-a2b1-8` → `linux-aarch64-a3-16-sh-001`; container `cann:9.1.0-a3-ubuntu22.04-py3.12`; `MAIN2MAIN_IMAGE_TAG` paired |
| csrc cache | `ensure-csrc-cache` `target_ids: '[]'` (x86 build would poison the ARM64 key); on-runner compile with `SOC_VERSION=ascend910_9391`, `MAX_JOBS=184`; new `Save vllm-ascend csrc cache` step |
| NPU health | `Check npu and CANN info` upgraded: HBM-occupancy scan → free chips exported as `available_devices`; run step pins `ASCEND_RT_VISIBLE_DEVICES` to them; zero free chips is fatal |
| scheduling env | `MAIN2MAIN_PAIR_ALIGNED_DEVICES: '1'` (dual-die pairing), `MAIN2MAIN_TEST_TIMEOUT: '3600'` (backstop) |
| cases | `MAIN2MAIN_CASES_FILE` + Load step REMOVED — case selection lives in the flow repo's `test_policy.json` allowlist (15 cases, makespan-pinned). 方案 A. |
| deps | apt `ripgrep` (adapter fix sessions need `rg`) |
| opencode | custom DeepSeek provider in `~/.config/opencode/opencode.jsonc` (`api.deepseek.com`, model `deepseek-v4-flash`) instead of built-in `auth.json` |
| flow clone | default branch + `git fetch`/`reset --hard origin/main` refresh guard (resident runner must not pin a stale clone) |
| resolve-source | fetch retries (upstream + baseline); baseline transport failure falls back to fresh mode with a loud warning instead of silently discarding the accumulated state |
| kept from upstream | manual-review issue + Chain next run steps; `concurrency: main2main`; `MAIN2MAIN_BASELINE_REF: main2main_baseline` |

Validation-only artifacts that were stripped: scratch
`MAIN2MAIN_BASELINE_REF: main2main_baseline_test`, flow clone pinned to
`feature/a3-16-internal`, concurrency group rename.

## Prerequisites before applying

1. **Flow repo first**: `main2main_flow` `main` (a3-16 architecture) must be
   pushed — the WF clones the default branch.
2. **Secrets**: `PAT_TOKEN` unchanged; `MAIN2MAIN_API_KEY` must hold a
   **DeepSeek platform key** (provider and key must match — a Zhipu key
   against this provider fails auth, run 33886011133).
3. **Runner**: confirm `linux-aarch64-a3-16-sh-001` is registered and visible
   to the vllm-project org.
4. **Image**: confirm `swr.cn-southwest-2.myhuaweicloud.com/base_image/ascend-ci/cann:9.1.0-a3-ubuntu22.04-py3.12` exists.
5. Optional cleanup in the same PR: delete
   `.github/workflows/scripts/main2main_tests.json` (no longer read).

## Apply

Copy this file over `.github/workflows/schedule_main2main.yaml` on the
vllm-ascend default branch, then smoke it with:

```bash
gh workflow run schedule_main2main.yaml --repo vllm-project/vllm-ascend \
  -f target_commit=<40-char vllm sha>
```

First dispatch pays the on-NPU csrc compile (cache cold); subsequent runs
restore from `runs-on/cache` unless csrc inputs change.
