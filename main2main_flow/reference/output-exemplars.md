# Output Exemplars

Copy these shapes exactly; keep entries this terse.

## step_summary.md — adapted-step entry

- step-12: Adapted — vllm_ascend/platform.py, vllm_ascend/worker/worker.py
  Upstream commit: 3775d5fc
  Cause: Platform gained abstract method get_infinity_values(); worker init
  now passes its result to the sampler.
  Change: Implemented get_infinity_values() in AscendPlatform with NPU-safe
  dtypes; no guard — the added method is inert on the release version.

## step_summary.md — no-op entry

- step-13: No-op — upstream change is ROCm-only (vllm/platforms/rocm.py)

## analysis.md skeleton (6 lines)

# step-12 analysis
- Upstream contracts changed: <symbols/files from upstream.patch>
- vllm-ascend dependents checked (File Mapping Table): <files>
- Checked but unchanged: <file — why unaffected>
- Changes made / no-op rationale: <1-3 lines>
- Guard decision: <vllm_version_is("<tag>") used | no guard — why>

## review.md skeleton (5 lines)

# step-12 review
- Diff reviewed: <files>
- Guard direction + branch signature equality: OK / <issue>
- Imports verified against the vllm tree: OK / <issue>
- Remaining risks: none / <risk>

## result.json

```json
{"status": "adapted", "files_touched": ["vllm_ascend/platform.py", "vllm_ascend/worker/worker.py"]}
```

For a no-op step: `{"status": "noop", "files_touched": []}`
