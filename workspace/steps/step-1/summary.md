Step-1 Adaptation Review — No-Op Verified.

## Upstream Patch
The upstream patch (commit f5d3dc7115cf77472ba5e274f6becbbeddbf4bd5, titled '[Model Runner v2] Support update_config (#42783)') adds a new `update_config` lifecycle method to the v2 `GPUModelRunner` in `vllm/v1/worker/gpu/model_runner.py`. The method delegates to the v1 `GPUModelRunner.update_config()` then syncs `self.vllm_config.model_config` and `self.vllm_config.load_config`.

## Why No vllm-ascend Adaptation Is Needed
Both `NPUModelRunner` classes (in `vllm_ascend/worker/model_runner_v1.py` and `vllm_ascend/worker/v2/model_runner.py`) subclass `GPUModelRunner` (the v2 class) and inherit `update_config` automatically. The inherited method is not GPU-specific — it performs pure config data management (delegate to v1 helper, then sync vllm_config attributes). `self.ascend_config` holds a reference to the same `vllm_config` object, so writes to `self.vllm_config.model_config` are automatically reflected without any separate sync. No override, no version guard, and no patch update is required.

## Additional Patch Files
The upstream patch also touches MoE/quantization files (fused_moe config, marlin_moe, int_wna16 oracle, auto_gptq, awq_marlin) — these were verified as having zero vllm-ascend symbol references; AWQ is blocked on non-CUDA; string-based class name checks in fused_moe reference unchanged names. No adaptation needed.

## Verified Items
- All files in the change plan were assessed (model_runner_v1.py, v2/model_runner.py, patch/ directory)
- No required file was missed — inheritance covers `update_config` automatically
- No vLLM source files were modified
- No temp files left in the repo
- The only diff is mechanical commit reference updates in CI/config files (Dockerfile.lint, pr_test_full.yaml, pr_test_light.yaml, docs/source/conf.py) — not vllm-ascend source changes
- No version guards needed — inherited method, no override, no caller sites in vllm-ascend