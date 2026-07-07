# Upstream Paths That Rarely Need Ascend Adaptation

Advisory routing list: verify quickly, then no-op — still record the decision
in analysis.md. Maintainers may also feed entries to `MAIN2MAIN_PLAN_EXCLUDES`
to exclude them from step planning deterministically.

## Usually safe to no-op after a quick check
- `vllm/platforms/rocm.py`, `tpu.py`, `xpu.py`, `cpu.py`, `zen_cpu.py`,
  `cuda.py` — other platforms' implementations; Ascend ships its own
  `NPUPlatform` (`vllm_ascend/platform.py`).
- `vllm/v1/attention/backends/flash_attn*.py`, `flashinfer.py`, `rocm_*.py`,
  `triton_attn*.py` — GPU-specific backends; Ascend registers its own under
  `vllm_ascend/attention/`.
- `vllm/kernels/` (triton/helion GPU kernels) — Ascend kernels live in
  `vllm_ascend/ops/` (incl. `vllm_ascend/ops/triton/`).
- `vllm/distributed/device_communicators/pynccl*.py`, `quick_all_reduce.py` —
  NCCL-specific; Ascend uses pyhccl
  (`vllm_ascend/distributed/device_communicators/`).
- `csrc/`, `benchmarks/`, `docker/`, `docs/`, `examples/`, `tests/` at the
  vllm repo root — CUDA sources, docs, and test-only trees; no plugin contract.

## Look-similar paths that DO matter
- `vllm/platforms/interface.py` — the Platform ABC that AscendPlatform
  subclasses; abstract-method changes break instantiation at runtime.
- `vllm/v1/attention/backends/registry.py`, `.../backends/utils.py` — imported
  by `vllm_ascend/attention/attention_v1.py` and
  `vllm_ascend/attention/utils.py`.
- `vllm/v1/attention/backends/mla/` — `vllm_ascend/patch/platform/`
  `patch_mla_prefill_backend.py` and `vllm_ascend/attention/mla_v1.py` depend
  on its interfaces.
- `vllm/triton_utils` — provides `HAS_TRITON`, imported across vllm_ascend.
