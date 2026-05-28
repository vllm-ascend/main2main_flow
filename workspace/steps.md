# main2main Step Plan

**Base:** `1ac10f159a09897baada01b14b6a0dd6442aefd6`
**Target:** `33e94fc3adcb888a8c6d62285f70bc78fb068ec7`
**Commits:** 285  |  **Steps:** 75

## step-1 (commits: 2, vllm: 530 lines, total: 622 lines)

- Categories: ignored, vllm
- Range: `1ac10f15..78e7a7b9`

  - `f5d3dc71` [Model Runner v2] Support update_config (#42783)
  - `78e7a7b9` Refactor AWQ Marlin MoE onto modular WNA16 oracle (#42483)

## step-2 (commits: 7, vllm: 860 lines, total: 1807 lines)

- Categories: ignored, vllm
- Range: `78e7a7b9..6859ca76`

  - `4a39b4f5` [Model] Add Apertus Tool Parser (#41154)
  - `47829b11` [Bugfix] mamba: run single-token extends as decodes (#42430)
  - `e2673697` [Model Runner V2] Fix prompt logprobs calculation `Sizes of tensors must match` error (#42778)
  - `b12745e4` Fix `--convert` passed without `--runner` on causal models (#42935)
  - `8c296de6` [Perf] Re-enable flashinfer autotune by default and cleanup (#42857)
  - `67f58ce2` [Bugfix] Fix DSV4 MTP after ROCm mHC integration (#42930)
  - `6859ca76` [Bugfix] fix swiglu limit issue for humming backend + deepseek v4 (#42541)

## step-3 (commits: 5, vllm: 483 lines, total: 1069 lines)

- Categories: ignored, vllm
- Range: `6859ca76..8fc1c284`

  - `a2c8fc66` [ROCm][Quantization][3/N] Refactor quark_moe w4a4 w/ oracle (#41436)
  - `9758a6e5` [BugFix] support PP for Cohere vision model (#42819)
  - `00e20e76` [Refactor] Remove dead cuda kernels (#42767)
  - `ce88f01c` [Docs] update attribution to reflect EDEN foundation (#41666)
  - `8fc1c284` [ROCm] Guard AITER GDN decode fast path by layout (#42880)

## step-4 (commits: 8, vllm: 963 lines, total: 1396 lines)

- Categories: ignored, vllm
- Range: `8fc1c284..239b5ff3`

  - `84747489` Tier offload followup (#42529)
  - `cd49a05d` [Refactor] Remove dead code (#42889)
  - `01913548` [Perf][MLA] Enable FULL cudagraph capture for TRITON_MLA decode (#42885)
  - `57fef4e0` [Refactor] Extract shared coerce_to_schema_type utility from Minimax M2 tool parser (#43006)
  - `37ece593` [Perf] Padded nvfp4 quant kernel to remove additional copy, 2.4%~5.7% e2e performance improvement (#42774)
  - `a171e6b5` Add parallel drafting to v2 model runner unsupported features (#43010)
  - `f85c76d7` [CI/Build] Bump nvidia-cutlass-dsl to 4.5.1 (#42991)
  - `239b5ff3` [Frontend] Add --spec-method/--spec-model/--spec-tokens CLI aliases (#42476)

## step-5 (commits: 1, vllm: 4543 lines, total: 4545 lines)

- Categories: ignored, vllm
- Range: `239b5ff3..287471b9`

  - `287471b9` [Model Refactoring] Migrate DeepSeek V4 to vllm/models/ [1/N]  (#43004)

## step-6 (commits: 5, vllm: 266 lines, total: 338 lines)

- Categories: ignored, vllm
- Range: `287471b9..fba010dd`

  - `afd7b1dc` [Bugfix] Use platform-agnostic device in example_connector load (#42926)
  - `8f16c4a5` [BugFix][CPU][Spec Decode] Fix Eagle implementation on CPU backend (#42468)
  - `36dcaf25` [XPU] add gptq(int4) support (#37844)
  - `da03e549` [UX] Add a persistent cache for FlashInfer autotuning (#42537)
  - `fba010dd` [Bugfix][MRV2] Fix KVCache tensor explicit `kernel_block_size` dim (#42766)

## step-7 (commits: 1, vllm: 3258 lines, total: 3261 lines)

- Categories: ignored, vllm
- Range: `fba010dd..87b08c5f`

  - `87b08c5f` [Model Refactoring] Move DeepSeek V4 layers to `models/deepseek_v4/` [2/N] (#43039)

## step-8 (commits: 8, vllm: 551 lines, total: 5642 lines)

- Categories: ignored, vllm
- Range: `87b08c5f..257af77b`

  - `3ca8db2e` add cutedsl dsv4 indexer fp8 kernel (#42899)
  - `fab07e4d` [Bugfix][KV Connector] Fix SimpleCPUOffloadScheduler TOCTOU between Phase A and Phase B (#42289)
  - `6e889b58` [ci] Route 28 gpu_1_queue tests to h200_35gb queue (#43030)
  - `27f4ba94` fix: use keyword arguments for shard_id and expert_id in weight_loade… (#42671)
  - `9fd8487d` [Docs] Add SVG images for pooling models. (#42626)
  - `f1e3f0e6` [XPU] Use custom op collective behavior  (#41354)
  - `4a4fdabe` [Misc] Aligning tokwise pooler heads for consistency (#43041)
  - `257af77b` [Docs] Reorganize online serving docs. (#41907)

## step-9 (commits: 1, vllm: 1155 lines, total: 1158 lines)

- Categories: ignored, vllm
- Range: `257af77b..301d9864`

  - `301d9864` [Frontend] Consolidate beam search by BeamSearchMixin. (#42946)

## step-10 (commits: 1, vllm: 6380 lines, total: 6397 lines)

- Categories: ignored, vllm
- Range: `301d9864..b14be81c`

  - `b14be81c` [Model Refactoring] Move deepseek_v4_ops to models/deepseek_v4 [3/N] (#43073)

## step-11 (commits: 2, vllm: 908 lines, total: 2309 lines)

- Categories: ignored, vllm
- Range: `b14be81c..056bc2e1`

  - `f34623bf` [bug] AsyncScheduler drops first post-resume token after pause_generation + clear_cache (#42117)
  - `056bc2e1` [KVConnector][DSV4] HMA support for Mooncake store connector (#42828)

## step-12 (commits: 1, vllm: 4276 lines, total: 4278 lines)

- Categories: ignored, vllm
- Range: `056bc2e1..07beaed8`

  - `07beaed8` [Model Refactoring] Rename deepseek_v4.py to model.py [4/N] (#43077)

## step-13 (commits: 6, vllm: 977 lines, total: 1402 lines)

- Categories: ignored, vllm
- Range: `07beaed8..1c615808`

  - `ef54a4d6` [Misc][MM] Remove redundant code in CLIPAttention (#43046)
  - `129019f3` [CI] Add MTP + PD disagg test for Qwen3.5 (#42677)
  - `a78b842d` [Bugfix] Fix top logprobs token placeholders in `/inference/v1/generate` (#42887)
  - `b82e908b` [Perf][4/n] Eliminate various GPU<->CPU syncs (#42347)
  - `d740e2c0` [XPU] update xpu graph usage (#43043)
  - `1c615808` [Model] Openvla support (#42654)

## step-14 (commits: 1, vllm: 161 lines, total: 229 lines)

- Categories: ignored, vllm
- Range: `1c615808..42b4f1fd`

  - `42b4f1fd` [Refactor] Extract extract_types_from_schema utility from Minimax M2 tool parser (#43025)

## step-15 (commits: 1, vllm: 23 lines, total: 30 lines)

- Categories: ignored, requirements, vllm
- Range: `42b4f1fd..8200fbe1`

  - `8200fbe1` [Misc] add humming to dependencies (#42540)

## step-16 (commits: 10, vllm: 356 lines, total: 722 lines)

- Categories: ignored, vllm
- Range: `8200fbe1..39bba710`

  - `d247a931` [feat] Add FP8 per-tensor Q scale support to Triton attention backend (#42080)
  - `aed2eb35` [Docs] Fix MooncakeStoreConnector role in disaggregated example (#42994)
  - `f54721bc` [Bugfix][MoE] FlashInfer one-sided: workspace union across heterogeneous layers (#42976)
  - `9aaf83ef` [CI failure] Temporarily disable using persistent cache for flashinfer autotune (#43119)
  - `a65093c1` [ci] Move language models tests (hybrid) back to L4 (#43129)
  - `12421962` [Model] Support post-norm architecture for EAGLE-3 supeculators (#42764)
  - `117afeea` Fix error in Dynamic NTK scaling (#41277)
  - `be167859` [CPU][DOC] Fix installation commands for Arm CPUs (#43115)
  - `73dd2f33` [bug] fix WeightTransferConfig.backend to allow for all strings (#43121)
  - `39bba710` [MRV2][BugFix] Fix default-stream CG capture in P/W LoRA case (#43160)

## step-17 (commits: 4, vllm: 194 lines, total: 250 lines)

- Categories: ignored, vllm
- Range: `39bba710..2ae910ed`

  - `5774aaed` [Cohere] Enable Cohere MoE (#43143)
  - `c628a93a` [Perf][Bugfix] Update dflash aux layer indexing (#40727)
  - `fadf5d33` add enqueue all option to throughput benchmark (#42975)
  - `2ae910ed` [Perf] Avoid forward scan for async output placeholders (#42938)

## step-18 (commits: 1, vllm: 0 lines, total: 13 lines)

- Categories: ignored, requirements
- Range: `2ae910ed..cd0ff26e`

  - `cd0ff26e` [CI] Add DSV4-Flash to gsm8k moe-refactor/config-b200.txt (#42111)

## step-19 (commits: 4, vllm: 22 lines, total: 331 lines)

- Categories: ignored, vllm
- Range: `cd0ff26e..40651c02`

  - `4f940896` [KV Offload] Pass `OffloadingSpec` instead of `VllmConfig` to secondary tiers (#43076)
  - `85959567` [ci] Revert model executor test back to L4 (#43188)
  - `7e4bc2ce` [Docs][PD][NIXL] Lease extension mechanism for blocks on P (#43099)
  - `40651c02` [Docs][PD][NIXL] Bidirectional kv-cache transfer (#43097)

## step-20 (commits: 1, vllm: 5 lines, total: 11915 lines)

- Categories: ignored, requirements, vllm
- Range: `40651c02..07aeaf9d`

  - `07aeaf9d` [6/n] Migrate activation kernels, gptq, gguf, non cutlass w8a8 to libtorch stable ABI (continued) (#42663)

## step-21 (commits: 10, vllm: 298 lines, total: 428 lines)

- Categories: ignored, vllm
- Range: `07aeaf9d..644b2a28`

  - `9b343dd4` Enable mermaid diagrams in the docs (#43192)
  - `1cb22443` [GDN] Enable FI Blackwell GDN prefill kernel (#40717)
  - `6f21558d` [XPU][CI] Add 2 server model test files in Intel GPU CI (#42499)
  - `cb600d1c` [Frontend] Forward X-data-parallel-rank header on /inference/v1/generate (#42330)
  - `87e31455` [Doc] Sync CLI guide with actual help modes and launch subcommand (#40326)
  - `19cf3342` [Feature] Support manually enabling the cumem allocator (#33648)
  - `0a508743` [Spec Decode] Support non-MTP speculation for NemotronH (#43130)
  - `df84fb07` Remove additional dead code as a follow-up to #42889 (#43144)
  - `ded87120` [Bug][Structured Outputs] Fix bug that leads to unconstrained generations with structural tags (#42452)
  - `644b2a28` [Bugfix] Use enable_sm120_family for per-tensor FP8 CUTLASS kernels on SM12.1 (#41215)

## step-22 (commits: 10, vllm: 716 lines, total: 1660 lines)

- Categories: ignored, vllm
- Range: `644b2a28..6dc0a718`

  - `a10d6911` [Bugfix] Use shared coerce_to_schema_type in DeepSeekV32 tool parser (#43019)
  - `9c78c999` [MISC] Fix symm_mem cap-equal gate; log AR backend selection (#42993)
  - `2d6b3489` [R3] Add routed experts to openai entrypoint  (#38939)
  - `f2d5e3d3` [CI] Lower granite-4.0-h-tiny gsm8k threshold for Hybrid SSM NixlConnector PD accuracy tests (4 GPUs) (#43186)
  - `363fc844` Integrate flashinfer b12x MoE and FP4 GEMM kernels for SM120/121 (#40082)
  - `53ff50fc` [Perf] Optimize `CutlassFP8ScaledMMLinearKernel` when padding needed by pre-weight processing, 13.5% TTFT improvement (#42651)
  - `2a43b407` [Bugfix][CI] Add missing import of pad_nvfp4_activation_for_cutlass in flashinfer (#43237)
  - `452baa86` Add dllehr-amd to CODEOWNERS and committers list (#42772)
  - `5774aad9` [Perf][gpt-oss] Downgrade triton_kernels to v3.5.1 (#43135)
  - `6dc0a718` [Misc] downgrade nvidia-cutlass-dsl to 4.5.0 (#43230)

## step-23 (commits: 10, vllm: 566 lines, total: 1188 lines)

- Categories: ignored, vllm
- Range: `6dc0a718..6441cf4a`

  - `bde560ed` [ROCm] Add QuickReduce min-size override and codec threshold (#41675)
  - `63ea1170` [CI] Add composed-schema regression tests for DeepSeek V3.2/V4 parsers (#43255)
  - `9640970d` [Model Runner V2] Fix lora `Triton Error [CUDA]: device-side assert triggered` (#43139)
  - `5d041cc1` update GPU json file based on h200 recipes (#43262)
  - `ee05e813` [Minor]  Bigger overlap for FI AR (#43103)
  - `e45df8c3` [Bugfix] Fix Qwen3.5 GatedDeltaNet in_proj_ba Marlin failure at TP>=2 (#36329)
  - `2b75a73b` [Perf][Gemma4] Batch vision encoder calls for image and video processing (#43169)
  - `7e507093` [CI] Fix "test_vit_cudagraph_[image|video][step3_vl]" failure (#43082)
  - `346cf163` [Frontend] Normalize reasoning_content to reasoning for client compatibility (#42664)
  - `6441cf4a` [Refactor] Use shared coerce_to_schema_type in Seed-OSS tool parser (#43140)

## step-24 (commits: 1, vllm: 117 lines, total: 301 lines)

- Categories: ignored, vllm
- Range: `6441cf4a..d97ba29f`

  - `d97ba29f` [ToolParser][Bugfix] Re-land: Fix anyOf/oneOf/$ref type resolution in Qwen3CoderToolParser (#37831) (#38973)

## step-25 (commits: 1, vllm: 331 lines, total: 984 lines)

- Categories: ignored, requirements, vllm
- Range: `d97ba29f..f2ace1d5`

  - `f2ace1d5` [Frontend][RFC] Rust front-end integration (#40848)

## step-26 (commits: 10, vllm: 461 lines, total: 671 lines)

- Categories: ignored, vllm
- Range: `f2ace1d5..caf69823`

  - `a6682d1d` [Bugfix] Warn when renderer_num_workers has no effect on offline LLM (#42905)
  - `905b97ad` [Benchmark] Add num-warmup to vllm bench throughput (#43245)
  - `050611a3` [Bugfix] Fix glm4_moe_tool_parser._is_string_type for /v1/responses FunctionTool format (#39601)
  - `a950e944` [CI] De-flake test_models for bigscience/bloom-560m (#43197)
  - `0a54df28` [XPU] add setuptools-rust for xpu dependency (#43287)
  - `b719b163` Update KDA chunk prefill decay to use exp2 semantics (#43195)
  - `edafea35` Fix FlashInfer TRTLLM NvFP4 monolithic MoE routing (#43223)
  - `ebbfb34e` [Test] Replace zephyr-7b-beta (7B) with SmolLM2-135M in tokenization test (#43085)
  - `68e07d59` [Bug] Fix ci issue `assert output_size is not None` AssertionError (#43261)
  - `caf69823` [CI] Pin protoc binary in rust-build stages (#43292)

## step-27 (commits: 5, vllm: 861 lines, total: 3212 lines)

- Categories: ignored, vllm
- Range: `caf69823..1c78f76c`

  - `5ecd8e9c` [XPU][CI]Fix Docker image pull-to-run race in Intel GPU CI (#43266)
  - `c68c55d4` [CPU][RISC-V] Add VLEN=256 support to RVV attention kernels (#42943)
  - `b730c463` [Perf] [Hybrid] Fused Triton kernel for GPU-side Mamba state postprocessing (#40172)
  - `9b9d5dba` [CI] Fix CPU tests failing on `tl.exp2` import (#43311)
  - `1c78f76c` [Bugfix] Add early validation to reject incompatible runner types for embedding models (#43079)

## step-28 (commits: 3, vllm: 256 lines, total: 389 lines)

- Categories: ignored, vllm
- Range: `1c78f76c..17b69828`

  - `9b54e50e` [Deprecation] Mark env vars covered by --moe-backend / --linear-backend (#43148)
  - `b29cbf06` [Perf] `zeros` -> `empty` to remove additional fill (#42988)
  - `17b69828` [Core] Add native ModelExpress load format (#43105)

## step-29 (commits: 1, vllm: 0 lines, total: 12 lines)

- Categories: ignored, requirements
- Range: `17b69828..0b59fc45`

  - `0b59fc45` Disable build isolation to bypass CUDA related deps for vllm-tpu (#43038)

## step-30 (commits: 4, vllm: 176 lines, total: 272 lines)

- Categories: ignored, vllm
- Range: `0b59fc45..39d5fa96`

  - `0f66623b` [Frontend] Rework fastokens integration (#43168)
  - `e26e1f09` [Feature] Add `--cpu-distributed-timeout-seconds` CLI Option for CPU Process Group Timeout (#42968)
  - `565b745e` [BugFix] Use correct logprobs for `logprob_token_ids` (#43125)
  - `39d5fa96` [Bugfix] Zero stale is_prefilling in padded CUDA graph rows for Mamba (#41873)

## step-31 (commits: 1, vllm: 0 lines, total: 72659 lines)

- Categories: ignored, requirements
- Range: `39d5fa96..39910f2b`

  - `39910f2b` [Rust Frontend] Move code from `vllm-frontend-rs` (#43283)

## step-32 (commits: 7, vllm: 709 lines, total: 1439 lines)

- Categories: ignored, vllm
- Range: `39910f2b..18a27cc9`

  - `ba369b7e` [CI] Fix dockerfile dependency graph failure for pre-commit (#43378)
  - `2998a047` [Bugfix] Fix DSV4 Base model swiglu limit issue in FP8 path  (#42855)
  - `86ccef7d` [ROCm] Add XGMI backend for MoRI Connector (#41753)
  - `35d0141a` [ROCm][CI] add warmup to mem_util test before measurement (#43236)
  - `60af5c16` [Frontend] Add truncation side to OpenAI endpoints (#43260)
  - `0ddd7dd6` [Frontend] DP Supervisor (#40841)
  - `18a27cc9` [Bugfix] Make CuMemAllocator free callback stream-aware (#43020)

## step-33 (commits: 7, vllm: 753 lines, total: 965 lines)

- Categories: ignored, vllm
- Range: `18a27cc9..6bb8753d`

  - `8c8b1825` [XPU] Enable multiple key kernels for sparse attention (#37888)
  - `1fe33039` [CI] De-flake renderers/test_hf.py::test_resolve_content_format_fallbacks[Qwen/Qwen-VL-string] (#43064)
  - `e746a2ee` [Model] Use `AutoWeightsLoader` for Voyage (#42972)
  - `fa1ff88b` [Model] Fix MiniCPM-V 4.6 vit_merger qkv weight loading (#43213)
  - `5ea76fa8` [CI] Fix test_lora_with_spec_decode on V2 model runner (#43314)
  - `025d4f5c` [CI] Fix "test_awq_load[gemma4-moe-*]" failure (#43296)
  - `6bb8753d` Correcting the mock classes for MM GC tests (#43321)

## step-34 (commits: 1, vllm: 0 lines, total: 3 lines)

- Categories: ignored, requirements
- Range: `6bb8753d..694d9a81`

  - `694d9a81` [BugFix] Fix setuptools-rust dep in requirements files (#43377)

## step-35 (commits: 2, vllm: 0 lines, total: 17 lines)

- Categories: ignored
- Range: `694d9a81..2380bfc2`

  - `a7616977` Fix the docker build failure in tpu-inference (#43360)
  - `2380bfc2` [Docs] Note image preprocessing difference between qwen_vl_utils and vllm. (#43393)

## step-36 (commits: 1, vllm: 152 lines, total: 182 lines)

- Categories: ignored, requirements, vllm
- Range: `2380bfc2..65b7a812`

  - `65b7a812` [CPU] Experimentally enable Triton and MRV2 (#43225)

## step-37 (commits: 1, vllm: 5505 lines, total: 5505 lines)

- Categories: vllm
- Range: `65b7a812..7e1b45a0`

  - `7e1b45a0` [Attention] Mamba attention module refactor (#41126)

## step-38 (commits: 10, vllm: 701 lines, total: 1673 lines)

- Categories: ignored, vllm
- Range: `7e1b45a0..91f5b924`

  - `d3d1cf69` [XPU]feat: add XPU fallback for MoE topk routing and MXFP4 backend (#42951)
  - `b3c7ffca` [Misc] Replace assert with proper exceptions for security and validation in pooling (#43286)
  - `4658bf88` [Bugfix] Clear P0 mm sender cache on sleep/pause to fix mm_hash desync (#43001)
  - `79ff0ffa` [BugFix] wire make_empty_intermediate_tensors on AyaVision and Voxtral (#43118)
  - `15f7cd33` [LoRA] Reduce memory of 2D weights when EP is set (#42737)
  - `d3a56350` [EPLB] Change default EPLB communicator (#43110)
  - `a377631d` [CI] Fix AMD docker build tests (#43329)
  - `fb21d8b4` Add NVFP4 MOE support for Deepseek V4. (#42209)
  - `f0feb15e` [Multimodal] Simplify ViT CUDA graph interfaces (#41234)
  - `91f5b924` [Rust Frontend] [Refactor] Extract a newtype for utility call ID (#43405)

## step-39 (commits: 2, vllm: 52 lines, total: 287 lines)

- Categories: ignored, vllm
- Range: `91f5b924..b21f3d56`

  - `c7624bea` [Bugfix] Source num_qo_heads from Attention layers in Flashinfer/Triton metadata builders (#42650)
  - `b21f3d56` [KV Connector] MooncakeStore: don't co-queue save with load to avoid double delayed-free (#43371)

## step-40 (commits: 1, vllm: 4022 lines, total: 4033 lines)

- Categories: ignored, vllm
- Range: `b21f3d56..84371573`

  - `84371573` [Refactor] Extract DeepSeek V4 sparse MLA impl into model folder (#43149)

## step-41 (commits: 10, vllm: 592 lines, total: 1826 lines)

- Categories: ignored, vllm
- Range: `84371573..8de5cabe`

  - `2b94d1c0` [Frontend] Simplify AuthenticationMiddleware path extraction (#43426)
  - `977703aa` [RFC][EPLB][#32028] Remove dead torch.accelerator.synchronize() from sync path (#40733)
  - `23f7b11b` [Bugfix] Detect wrong libcute_dsl_runtime.so variant in FlashInfer GDN (#43427)
  - `4e597b74` [Bugfix] Clear error message for FP8 torchao quantization on unsupported GPUs (#36854)
  - `08cb4678` mhc_post - remove sts & add vectorized copies (#43437)
  - `e203006a` [Quantization][ModelOpt] W4A16 NVFP4 fused MoE + mixed-precision dispatch (#42566)
  - `47d4407d` [Model Runner V2] Support sharing kv cache layers (#35045)
  - `f7432541` DSv4 fused Q-norm kernel grid refactor (#42353)
  - `4e2eba28` [Perf] Optimize hidden state extraction logic (#37374)
  - `8de5cabe` [XPU]fix: add XPU platform guards to DeepSeek-V4 ops (#42950)

## step-42 (commits: 10, vllm: 759 lines, total: 986 lines)

- Categories: ignored, vllm
- Range: `8de5cabe..54d15363`

  - `6d30655b` elastic_ep: stage/commit MoE quant method on reconfigure (#40881)
  - `552bbe6f` [Attention] Add head_dim=512 support for FlashInfer trtllm attention backend (#38822)
  - `3cb83c95` Add `model` to `WeightTransferEngine.__init__` (#42922)
  - `367cb819` [DSV4] More multi-stream enablement for c4a (#42925)
  - `6a4723a2` [ROCm][CI] Stabilize runner teardown between sampler tests (#43023)
  - `76ea1d5d` [ROCm][CI] Stabilize Granite tool-use and test URL construction (#43017)
  - `84e35155` [Bugfix] Auto-raise max_num_batched_tokens for prefix-LM multimodal models (#43051)
  - `d28bdf93` [ROCm][CI] Fix ROCm LoRA Transformers fallback with full CUDA graphs (#41577)
  - `a5bbd81e` [XPU]feat: enable FP8 block-scaled quantization on XPU (#42952)
  - `54d15363` [XPU] reudce host overhead of XPU MOE (#42915)

## step-43 (commits: 10, vllm: 243 lines, total: 6138 lines)

- Categories: ignored, vllm
- Range: `54d15363..a0be71ee`

  - `a7be0f34` [7/n] Migrate pos_encoding and norm kernels to libtorch stable ABI (continued) (#43209)
  - `3a1c0621` [Misc] Added missing return type annotations to improve mypy and IDE tooling (#43383)
  - `d19db109` [Bugfix] Fix native Triton top-k/top-p kernel assumes contiguous logi… (#42739)
  - `09a219c0` [ModelOpt] Support Qwen3.5/3.6 VLM quantized prefix mapping (#42546)
  - `82536acc` Keep scheduler alive for delayed KV connector frees (#43433)
  - `3f3e8626` fix(eagle3): read norm_before_fc from eagle_config for NVIDIA checkpoint (#42143)
  - `5bb8d276` [Kernel] Batch invariant NVFP4 linear using cutlass (#39912)
  - `2a7d5b73` [ROCm][CI] Remove benchmarks test group and shard long test groups (#41669)
  - `d8b385b7` [Bugfix][Frontend] Fix input_audio parsing when uuid is present  (#43414)
  - `a0be71ee` [MM] Enable FlashInfer metadata support for Qwen2.5-VL vision attention (#42787)

## step-44 (commits: 3, vllm: 658 lines, total: 662 lines)

- Categories: ignored, vllm
- Range: `a0be71ee..4438b6e7`

  - `7c2ff1f8` [Docs] Fix stale version number in token_embed.md (#43488)
  - `8737e4a8` [Docs] Fix stale version number in token_classify.md (#43489)
  - `4438b6e7` [MoE] Migrate W4A8 CT to oracle kernel setup (#42680)

## step-45 (commits: 2, vllm: 370 lines, total: 520 lines)

- Categories: ignored, vllm
- Range: `4438b6e7..46f95b2e`

  - `819c610f` [Mooncake] Add metrics for MooncakeStoreConnector operations (#43392)
  - `46f95b2e` [ROCm][Critical] Fix the GDN import bug (#43486)

## step-46 (commits: 1, vllm: 23 lines, total: 30 lines)

- Categories: ignored, requirements, vllm
- Range: `46f95b2e..10d264a2`

  - `10d264a2` Revert "[Misc] add humming to dependencies" (#43492)

## step-47 (commits: 5, vllm: 244 lines, total: 593 lines)

- Categories: ignored, vllm
- Range: `10d264a2..59405908`

  - `b32fe416` [Bugfix] Fix reasoning dropped on streaming boundary deltas (#42691)
  - `33d7cbe0` [Model Runner v2] Force v1 runner for tests (#43233)
  - `0902d8e6` [KV Connector] Keep MooncakeStore full hits block-aligned (#43494)
  - `357fddf6` [kv_offload]: Add DSv4 support (#43142)
  - `59405908` [ROCm][CI] Stabilize 400 error return code for invalid schema inputs (#43016)

## step-48 (commits: 1, vllm: 2392 lines, total: 2392 lines)

- Categories: vllm
- Range: `59405908..1806d1ad`

  - `1806d1ad` [ROCm] [DSv4] [Perf] Support DeepSeek v4 MTP (#43385)

## step-49 (commits: 1, vllm: 724 lines, total: 1861 lines)

- Categories: ignored, vllm
- Range: `1806d1ad..d56285c7`

  - `d56285c7` Tuning script and configs for Triton Mamba SSU kernel (#43083)

## step-50 (commits: 5, vllm: 996 lines, total: 2126 lines)

- Categories: ignored, vllm
- Range: `d56285c7..3df1c7c4`

  - `d0a100c8` File system secondary tier implemented in python (#41735)
  - `b06813e8` [Kernel] Add mhc_pre_big_fuse_with_norm_tilelang  (#43474)
  - `6cbe448e` fix: MoE model using shared routed experts crashes on AMD GPUs (#42373)
  - `1b26fa36` [Docs] Reorganize offline inference docs.  (#43552)
  - `3df1c7c4` [Docker] Non-root support for vllm-openai; add opt-in vllm-openai-nonroot target (#40275)

## step-51 (commits: 7, vllm: 337 lines, total: 1042 lines)

- Categories: ignored, vllm
- Range: `3df1c7c4..71d810bb`

  - `81252d4e` [Feat][KVConnector] Support DSV4 in SimpleCPUOffloadBackend (#42296)
  - `0c942c69` [Doc] Add section on escalating stalled contributions (#43568)
  - `5c1aec3d` Reduce memory usage for granite_speech. (#42933)
  - `873758c1` [KV Connector] Handle Mooncake finish after preemption (#43281)
  - `716d5294` [Misc] Print accuracy value for PD tests even on success  (#43583)
  - `d4004455` [Kernel] Remove NormGateLinear (#43554)
  - `71d810bb` [XPU] Ensure RNG offset alignment with PyTorch requirements in XPU sampler (#43028)

## step-52 (commits: 1, vllm: 1100 lines, total: 2060 lines)

- Categories: ignored, vllm
- Range: `71d810bb..ec5de7fa`

  - `ec5de7fa` [LoRA] Add one shot triton kernel For MoE LoRA (#42290)

## step-53 (commits: 4, vllm: 459 lines, total: 623 lines)

- Categories: ignored, vllm
- Range: `ec5de7fa..f815c999`

  - `aa2b56ff` [DeepSeek V4] Move MegaMoE input prep kernel to nvidia/ops (#43632)
  - `7966fc72` [KV Connector][Bugfix] MooncakeStore: don't double-apply Eagle prune in load_mask (#43516)
  - `c2a4005c` [KV Connector] Propagate MooncakeStore load failures (#42788)
  - `f815c999` [Bugfix] fix device mismatch in MiniCPM-o-4_5 resampler (#43194)

## step-54 (commits: 1, vllm: 1351 lines, total: 1355 lines)

- Categories: ignored, vllm
- Range: `f815c999..d5cf7b4a`

  - `d5cf7b4a` [Frontend] Split the offline inference APIs and utils. (#43553)

## step-55 (commits: 1, vllm: 9 lines, total: 9 lines)

- Categories: vllm
- Range: `d5cf7b4a..6f955986`

  - `6f955986` [Bugfix][Model] Fix GPT2ForSequenceClassification sub-module prefix (#43579)

## step-56 (commits: 1, vllm: 3450 lines, total: 3649 lines)

- Categories: ignored, vllm
- Range: `6f955986..d56612c6`

  - `d56612c6` [GDN] GDN Prefill kernel for SM100 (#43273)

## step-57 (commits: 2, vllm: 0 lines, total: 131 lines)

- Categories: ignored
- Range: `d56612c6..e6adbd78`

  - `771e1e48` [CPU] Enable non-divisible GQA for decode workitems in mixed batches (#43032)
  - `e6adbd78` Upgrade tpu-inference to v0.20.0 (#43394)

## step-58 (commits: 1, vllm: 1458 lines, total: 1458 lines)

- Categories: vllm
- Range: `e6adbd78..a37e4710`

  - `a37e4710` Add CuTe DSL sparse compressor support (#43584)

## step-59 (commits: 8, vllm: 566 lines, total: 1073 lines)

- Categories: ignored, vllm
- Range: `a37e4710..861b9776`

  - `b3269454` [chores][log] change registry log from `warning` to `debug` (#43045)
  - `97e4022c` [Bugfix] Apply fc_norm in Eagle3DeepseekV2 combine_hidden_states (#43482)
  - `755043cf` [KV Transfer] Enable HMA by default for connectors that support it (#41847)
  - `681d7dd3` [Misc][Refactor][ROCm] Convert MoRI-related envvars to extra config args (#43303)
  - `5d09f471` [Misc] Support interleaved custom image benchmark datasets (#43636)
  - `739af5c7` [Reasoning] [Bugfix] Reject invalid thinking_token_budget values (#43402)
  - `ebd0692f` [Model] Use AutoWeightsLoader for InternLM2 (#38278)
  - `861b9776` [XPU] Fix fused MoE LoRA kernel crash on XPU by using platform-agnos num_compute_units (#43646)

## step-60 (commits: 1, vllm: 23 lines, total: 39 lines)

- Categories: ignored, requirements, vllm
- Range: `861b9776..a970fb5a`

  - `a970fb5a` Fix CuPy runtime deps and restore humming (#43530)

## step-61 (commits: 2, vllm: 0 lines, total: 1187 lines)

- Categories: ignored
- Range: `a970fb5a..445ded18`

  - `d565357a` [Docs][ROCm] MoRI-IO Connector Usage Guide (#43603)
  - `445ded18` [ROCm][CI] Extend ROCm quick reduce coverage (#40990)

## step-62 (commits: 1, vllm: 1871 lines, total: 2264 lines)

- Categories: ignored, vllm
- Range: `445ded18..6ab6ffb4`

  - `6ab6ffb4` [Feat][DSV4] Fuse q pad into deepseek v4 fused kernel (#43162)

## step-63 (commits: 1, vllm: 244 lines, total: 244 lines)

- Categories: vllm
- Range: `6ab6ffb4..b226ddac`

  - `b226ddac` [MoE Refactor] Migrate ModelOptMxFp8FusedMoE to oracle (#42768)

## step-64 (commits: 1, vllm: 842 lines, total: 1138 lines)

- Categories: ignored, vllm
- Range: `b226ddac..f51bbc69`

  - `f51bbc69` [MoE Refactor] W4a8 int8 oracle (#42789)

## step-65 (commits: 5, vllm: 712 lines, total: 925 lines)

- Categories: ignored, vllm
- Range: `f51bbc69..49b48827`

  - `c8414a82` [ROCm] Remove MegaMoE integration in deepseek v4 (#43629)
  - `6f5b5332` Add LM head quantization support for ModelOpt (#42124)
  - `3aea37d2` [Doc] Add line limit to AGENTS.md (#43635)
  - `193ce881` [DSv4] Drop _get_compressed_kv_buffer in DeepseekCompressor (#43690)
  - `49b48827` [CI] Soft-fail AMD entrypoints mirror tests (#43709)

## step-66 (commits: 1, vllm: 1318 lines, total: 1356 lines)

- Categories: ignored, vllm
- Range: `49b48827..6e503868`

  - `6e503868` [Kernel] Porting  fuse_minimax_qk_norm  to manual fusion (#43410)

## step-67 (commits: 10, vllm: 844 lines, total: 2242 lines)

- Categories: ignored, vllm
- Range: `6e503868..c02c758e`

  - `d98cbf47` [KV Connector] MooncakeStore: drop dead discard_partial_chunks parameter (#43627)
  - `812e7e73` [Bugfix][V1] Fix TOCTOU race causing intermittent `EADDRINUSE` on multi-API-server DP startup (#42585)
  - `e19b9b10` [ci] Add arm64 ci image (#41303)
  - `dede691c` [Bugfix] Split attention groups by num_heads_q for spec-decode drafts (#43543)
  - `0b68f21e` [Rust Frontend] Add reasoning/tool parser & renderer roundtrip tests (#43582)
  - `5bdb181d` [ROCm][CI] Fix ROCm multimodal Qwen2.5-VL activation compile and Phi4MM ragged image mask handling (#43647)
  - `d8eebe6d` [Perf] Optimize Fp8BlockScaledMMLinearKernel input_scale tensor using new_empty() (#43677)
  - `7e33081c` [Attention] Make FlexAttention and FlashAttention use num-blocks first layouts (#42095)
  - `aa613816` [MLA][Attention] Add OOT MLA prefill backend registration mechanism (#43325)
  - `c02c758e` [Deprecation] Deprecate functions as scheduled for v0.21.0 (#43358)

## step-68 (commits: 1, vllm: 3077 lines, total: 3077 lines)

- Categories: vllm
- Range: `c02c758e..adaa5e45`

  - `adaa5e45` [DSv4] Refactor compressor & Fix ROCm compatibility (#43710)

## step-69 (commits: 4, vllm: 250 lines, total: 608 lines)

- Categories: ignored, vllm
- Range: `adaa5e45..8c94938c`

  - `0fa3114a` Fix test_aot_compile for torch 2.12 (#43695)
  - `1fc2cee5` [KVConnector][Mooncake] Wire reset_cache cascade end-to-end (#42694)
  - `7b546902` [ROCm][Perf] Expose AITER MoE sorting dispatch policy via env var (#39177)
  - `8c94938c` [MRV2][BugFix] Fix KV connector handling in spec decode case (#43719)

## step-70 (commits: 2, vllm: 839 lines, total: 1574 lines)

- Categories: ignored, vllm
- Range: `8c94938c..de12f5ca`

  - `683033d4` [Frontend] Add MiniCPM5 XML tool call parser (#43175)
  - `de12f5ca` [ROCm][GPT-OSS] Avoid repeated compile-time `cos_sin_cache.to(bf16)` casts in rotary path (#42833)

## step-71 (commits: 1, vllm: 0 lines, total: 11 lines)

- Categories: ignored, requirements
- Range: `de12f5ca..ad464e16`

  - `ad464e16` [Doc] Add Ascend NPU tab to the quickstart installation guide (#43550)

## step-72 (commits: 9, vllm: 146 lines, total: 2454 lines)

- Categories: ignored, vllm
- Range: `ad464e16..05c50c72`

  - `396c8fee` [Rust Frontend] Align tool parser fallback behavior between streaming & non-streaming paths (#43662)
  - `158289e0` [Docs] Fix MLA prefill backend default docs (#43697)
  - `22720624` [Kernel] Enable TritonW4A16LinearKernel as CUDA fallback for non-Marlin-aligned W4A16 shapes (#43731)
  - `52a31cce` [Bugfix] Map reasoning_effort to enable_thinking in chat template kwargs (#43401)
  - `03d9cc2f` [misc] Bump cutedsl version to 4.5.2 (#43745)
  - `16546094` [BugFix] HFValidationError with cloud storage URIs when HF_HUB_OFFLINE=1 (#39155)
  - `49a35102` [Docs] Fix the duplicate doc icon issue (#43546)
  - `41688e2d` Fix early CUDA init (#43791)
  - `05c50c72` [ROCm] mori: add InterNodeV1LL inter-node kernel selection via VLLM_MORI_INTERNODE_KERNEL (#41751)

## step-73 (commits: 1, vllm: 0 lines, total: 5221 lines)

- Categories: ignored, requirements
- Range: `05c50c72..284e6f54`

  - `284e6f54` [8/n] Migrate merge_attn_states, mamba, sampler to torch stable ABI (continued) (#43361)

## step-74 (commits: 10, vllm: 341 lines, total: 2085 lines)

- Categories: ignored, vllm
- Range: `284e6f54..05eec712`

  - `206b72c9` [Quantization] Fix Humming RoutedExperts import (#43540)
  - `2616f67f` Remove Transformers forward/backward compatibility tests (#43785)
  - `2c2c9666` Validate against some config fields being set to 0 (#43794)
  - `7fb9c019` [Bugfix][DFlash]allocate the proper number of lookahead slots (#43733)
  - `5963c194` Fix Qwen3-VL and Qwen3-omni-thinker accuracy degradation from deepstack inputs under torch.compile (#43617)
  - `094124af` Add @AndreasKaratzas to CODEOWNERS (#43740)
  - `381edde1` [Bugfix][Kernel] TRTLLM NVFP4 MoE chunking (#43599)
  - `1223732d` [ModelRunnerV2][Hybrid model] Support kernel block size in hybrid model (#38831)
  - `c87f62cc` [Rust Frontend] Introduce mock engine for benchmark baseline (#43469)
  - `05eec712` Fix RunAI streamer tensor buffer reuse during weight loading (#43464)

## step-75 (commits: 3, vllm: 225 lines, total: 326 lines)

- Categories: ignored, vllm
- Range: `05eec712..33e94fc3`

  - `2d2c6601` [MoE] Remove inplace fused experts mechanism (#43727)
  - `413ac5c0` [Misc][Rocm] Remove redundant `AiterUnifiedAttentionBackend` block size log (#43664)
  - `33e94fc3` [ROCm][CI] Stabilize Cargo cache and pre-test image checks (#43815)
