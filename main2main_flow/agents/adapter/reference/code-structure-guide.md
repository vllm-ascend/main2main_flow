# Code Structure Guide

Use this guide as a stable routing reference when mapping upstream vLLM changes
to likely vllm-ascend files. These tables describe code structure, not workflow
policy. Read only the sections needed for the current step.

## Index

| Section | Trigger — read when... | Lines |
|---------|------------------------|-------|
| vLLM Key Areas to Focus On | mapping an upstream change to likely vllm-ascend files | 21-82 |
| File Mapping (via vllm-report) | vllm-report unavailable; need manual fallback routing | 85-91 |

This file may need refreshing when vllm-ascend structure changes. On the final
main2main step, check whether the vllm-ascend files/directories or mappings below
became stale during the upgrade. If they changed, update this file to match the
current vllm-ascend structure.

---

## vLLM Key Areas to Focus On

When analyzing vLLM changes, pay special attention to these areas that typically
require vLLM Ascend adaptation:

<!-- BEGIN REFERENCE: key-areas -->
1. **Platform Interface** (`vllm/platforms/`)
   - New abstract methods — implement immediately; missing ones cause `TypeError: Can't
     instantiate abstract class AscendPlatform` at runtime, not at import time, so they
     won't surface until a test actually executes
   - Method signature changes
   - New platform capability flags

2. **Worker / Model Runner** (`vllm/v1/worker/`, `vllm/v1/worker/gpu/model_runner.py`)
   - New or removed parameters in `execute_model` or `load_model` — vllm-ascend heavily
     overrides these; signature mismatches cause `TypeError` during inference
   - New lifecycle methods
   - Changes to model runner initialization

3. **Attention** (`vllm/model_executor/layers/attention/`, `vllm/v1/attention/`)
   - New parameters in `forward()` — vllm-ascend registers its own backend; interface
     changes require updating both registration and implementation
   - Changes to attention backend interface
   - MLA-specific updates

4. **MoE** (`vllm/model_executor/layers/fused_moe/`)
   - FusedMoE layer signature changes — vllm-ascend has Ascend-specific MoE kernels
     that call into this interface
   - Router interface changes
   - Activation function changes

5. **Config** (`vllm/config*.py`)
   - Field renames or moves between config classes — vllm-ascend reads config fields
     directly in many places; a rename causes `AttributeError` everywhere it's accessed
   - New required fields
   - Constructor changes

6. **Distributed** (`vllm/distributed/`)
   - Changes to collective op interfaces
   - KV transfer protocol changes
   - Device communicator updates

7. **Speculative Decoding** (`vllm/v1/worker/gpu/spec_decode/`, `vllm/config/speculative.py`)
   - Import path changes
   - Config field changes
   - New proposer interface methods — vllm-ascend has MTP and Eagle proposer implementations

8. **Compilation** (`vllm/compilation/`)
   - Pass manager interface changes
   - New required passes
   - Changes to how passes register

9. **Quantization** (`vllm/model_executor/layers/quantization/`)
   - Quantization config changes
   - compress-tensor method changes

10. **Models** (`vllm/model_executor/models/`)
    - Changes to model forward signatures — when vllm-ascend overrides a model's
      forward method, signature changes break inference
    - New model architectures
<!-- END REFERENCE: key-areas -->

---

## File Mapping (via vllm-report)

The static File Mapping Table has been replaced by vllm-report's dynamic
`patch_impact_map` and `definitely_affected_paths`, which are injected
per-step as `{vllm_report_context}` in the Code Exploration section above.
vllm-report's maps are extracted from vllm-ascend's `patch/__init__.py`
registration code, so they reflect the actual patch wiring (not just
directory-level guesses).

When vllm-report is unavailable (clone failed or commit not covered), fall
back to the Key Areas and File Locations above to manually route changed
upstream paths to vllm-ascend files via grep.
