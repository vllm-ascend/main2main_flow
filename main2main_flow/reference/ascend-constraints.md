# Ascend Platform Constraints

NPU facts to know before porting an upstream (CUDA-first) change. Each bullet
cites the vllm-ascend file that evidences it.

## Runtime and devices
- The runtime is torch_npu on CANN, not CUDA: `requirements.txt` pins
  `torch-npu`; device type is `"npu"`, torch dispatch key `"PrivateUse1"`
  (`vllm_ascend/platform.py`).
- Runtime failures surface as ACL/HCCL `RuntimeError`s (ACL stream synchronize
  failed, `HcclCommInitRootInfo`, NPU out of memory), not CUDA errors.
- Device tiers: `AscendDeviceType` A2 / A3 / _310P / A5 in
  `vllm_ascend/utils.py`. Capability guards use helpers like `is_310p()`
  (`vllm_ascend/utils.py`), e.g. `if not is_310p():` around imports in
  `vllm_ascend/patch/worker/__init__.py`. 310P-only code lives in
  `vllm_ascend/_310p/`.

## Collectives
- Collectives run over HCCL; NCCL is unavailable on NPU
  (`vllm_ascend/patch/__init__.py`). Workers init the process group with
  backend `"hccl"` (`vllm_ascend/worker/worker.py`).
- The device communicator is `NPUCommunicator`
  (`NPUPlatform.get_device_communicator_cls` in `vllm_ascend/platform.py`);
  the pyhccl wrapper lives in
  `vllm_ascend/distributed/device_communicators/pyhccl.py`.

## Graph mode / compilation
- NPU captures ACL graphs, driven by vLLM's `CUDAGraphMode` config enum
  (`vllm_ascend/compilation/acl_graph.py`). Only `CompilationMode` NONE and
  VLLM_COMPILE are supported; other modes force `CUDAGraphMode.NONE`
  (`vllm_ascend/platform.py`, `check_and_update_config`).
- torchair/npugraph_ex are patched in under guards (`vllm_ascend/patch/worker/__init__.py`,
  `vllm_ascend/compilation/compiler_interface.py`); xlite is a config-gated worker
  selected in `vllm_ascend/platform.py` under `xlite_graph_config.enabled`.
- No logging calls on torch.compile paths — review-lessons §2.3.

## Triton
- Triton IS available on NPU via triton-ascend, but usage is always guarded
  with `from vllm.triton_utils import HAS_TRITON`
  (`vllm_ascend/ops/__init__.py`, `vllm_ascend/patch/worker/__init__.py`).
  Ascend triton kernels live in `vllm_ascend/ops/triton/`. Kernel call args
  must match the kernel signature — review-lessons §2.2.

## Attention backends
- Ascend registers its own attention backends via
  `vllm.v1.attention.backends.registry` (`vllm_ascend/attention/attention_v1.py`)
  and `NPUPlatform.get_attn_backend_cls` (`vllm_ascend/platform.py`); upstream
  flash-attn/flashinfer/triton backends never run on NPU.

## Dtypes
- bfloat16 is not supported by `torch.topk` under GE graph mode
  (`vllm_ascend/ops/fused_moe/experts_selector.py`).

## CUDA-only upstream features
- CUDA custom ops (`torch.ops._C.*`) do not exist on NPU; see
  error-pattern-examples "Custom Op Not Registered".
- When upstream hard-requires a CUDA-only component, register a minimal no-op
  stub plus a `# TODO` instead of porting it. Existing example:
  `vllm_ascend/patch/platform/patch_mla_prefill_backend.py` stubs
  `MLAPrefillBackend` because the upstream selector assumes flash_attn.
