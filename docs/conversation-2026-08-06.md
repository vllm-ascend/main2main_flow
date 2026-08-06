# main2main_flow 会话记录 — 2026-08-05/06

## 背景

本项目是 vllm-ascend 的 main2main 自动化适配流程。本次会话围绕 4 大主题：
1. PR 描述完整性（description-fill agent）
2. UT 质量门禁（_check_ut，CPU + A2 NPU + E2E smoke）
3. vllm-report MCP 动态调用修复（7 个 commit 的完整链条）
4. PR #13657 的 4 个问题分析（MCP init 假阴性、opencode 慢、UT 环境、PR 描述缺陷）

---

## 一、PR 描述完整性修复

### 背景问题
PR #13515 改了 38 个文件但描述只显示 2 个。根因：
- `generate_final_post` 读 per-step 的 `EACH_STEP_TARGET_PATCH_FILE`（增量、最后一次 retry 覆盖前几次）
- 累计补丁 `gate_final_patch`（`git diff original_ascend_ref`）存在但没用于描述生成

### 改动
- `flow.py` 新增 `_extract_diff_files()` + `_parse_summary_files()` helpers
- `generate_final_post` 改用 `FINAL_TARGET_PATCH_FILE`（累计）作为文件列表事实来源
- 按 step_summary.md 的 header（`- step-N: Adapted — <files>`）+ Change 字段反引号路径归属文件到 step
- 新增 `agents/description-fill/SKILL.md`（只读分析 role）+ `_fill_unattributed_analysis()` + `_parse_unattributed_entries()`
- `opencode_adapter.py` 的 `_build_prompt` 支持 `role="description-fill"`
- 只传 unattributed 文件过滤后的 patch 给 agent（省 token）

### 注意
- step_summary.md 里 adapter 写的文件**没有反引号**——`_parse_summary_files` 只提取反引号路径，这是个已知缺口

---

## 二、UT 质量门禁（_check_ut）

### 验证（CANN 容器）
- 容器: `swr.cn-southwest-2.myhuaweicloud.com/base_image/ascend-ci/cann:9.0.1-910b-ubuntu22.04-py3.12`（Python 3.12.13, aarch64）
- 安装: `uv pip install -r requirements-dev.txt` + `uv pip install -e .`（需 `SOC_VERSION=ascend910b1`）
- 测试: 158 CPU UT → 1861 passed + 1 failed + 22 skipped in 14.36s

### 实现（pre_ci_check.py）
- `_npu_available()` — 检测 `npu-smi info`
- `_collect_cpu_ut_files()` — 158 文件，跳过 `a2/` `a3_2/` 子目录
- `_collect_a2_npu_ut_files()` — 35 个 `tests/ut/*/a2/` 文件
- `_source_ascend_env()` — source ascend-toolkit/set_env.sh
- `_check_ut()` — 3 个 batch：CPU-UT (158, 30min) + A2-NPU-UT (35, 30min) + A2-NPU-E2E (test_extract_hidden_states.py, 25min)
- venv: `--system-site-packages` + numpy 1.26.4（从 triton-ascend metadata 动态读）
- `final_quality_gate.py` 接入，UT 失败阻断 push

### 关键发现
- `tests/ut/conftest.py` 在 npu-smi 不可用时 mock torch_npu/acl/mooncake——CPU runner 能跑 UT 的关键
- `test_batch_invariant.py` 全局 monkeypatch `torch.library.Library = MagicMock(...)` 且无 cleanup → 污染后续 test_gdn_layerwise_kv（aarch64 容器复现，amd64 CI 不复现）
- PR #13515 的 3 个 CI 失败：CPU UT minimax_m3（`get_current_vllm_config` 不在 context）、A2 NPU E2E test_extract_hidden_states（`No common block size for 16`）

---

## 三、vllm-report MCP 修复（7 个 commit 链条）

### 根因（PR #13515 的 run: 0 MCP 调用, 31 次 grep, 17min/attempt）
1. **SKILL.md 说"有 context 就 skip MCP"**（主因）——flow.py 每步预加载 151 行 static context，adapter 从不主动调 MCP
2. **配置文件名 `opencode.jsonc`** — opencode 项目根搜索只找 `opencode.json`
3. **MCP command 用直接文件路径** — 指南用 `python -m src.mcp_server_app`

### 修复（6 个 commit，合并成 50cbb83）
| Commit | 修复 |
|---|---|
| dcd5b65 | SKILL.md: MCP 是 PRIMARY，grep 是 FALLBACK |
| 4b776bf | 项目根配置 → `opencode.json` + `OPENCODE_CONFIG` env |
| ddeaa3d | command → `python -m src.mcp_server_app` + `cwd` 字段 |
| cf11385 | CI 日志 `[MCP] →/←` 标记（`_print_event` 按 `_BUILTIN_TOOLS` 集合区分 MCP/builtin）|
| 571f459 | 全局配置 `~/.config/opencode/opencode.jsonc`（合并写，不覆盖用户配置）|
| a841670 | init 时验证 MCP server 启动（8s timeout）|

另外删除了 `main2main_flow/scripts/utils/vllm_report.py`（343 行死代码——切到动态 MCP 后不再被调用）

### PR #13657 的验证结果
- **MCP 动态调用已生效**：`[MCP] ← vllm-report_get_adaptation_guide returned 27 lines`、`[MCP] ← vllm-report_get_cross_project_mapping returned 155 lines`（steps 1-4 都有调用）
- **但 init 验证假阴性**：`[init] WARNING vllm-report MCP server FAILED to start (exit=0)`——stdio server 读到关闭的 stdin 就退出 0，`subprocess.run` 拿不到"存活"信号。需改 `Popen(stdin=PIPE)` 保持 stdin 打开

---

## 四、PR #13657 的 4 个问题（待修复）

### 问题 1: MCP init 假阴性
- 现象: `[init] WARNING vllm-report MCP server FAILED to start (exit=0):`（但 MCP 调用正常）
- 根因: `subprocess.run(capture_output=True, text=True)` 子进程继承 CI 的关闭 stdin，stdio server 读 EOF 退出 0
- 修复: `Popen(stdin=PIPE)` + 检查进程存活

### 问题 2: opencode 分析慢（每 step 20-30 min）
```
step-1 attempt 1:  17:05:02 → 17:33:23  = 28 min
step-1 retry:      17:51:35 → 17:57:33  =  6 min（E2E 失败触发）
step-2:            18:13:38 → ~18:36    = 23 min
```
- 待分析: workspace 里 opencode_raw.jsonl 的 grep 次数、思考间隙（之前 8/5 run: 31 grep, 最大思考间隙 116s）

### 问题 3: UT 大面积失败（131 个）
- CPU-UT (14): MagicMock 断言失败（mock 泄漏污染）+ swiglustep UB 对齐 + test_gdn_layerwise_kv 污染
- A2-NPU-UT (117): `atten_mask is on cpu, different from other tensors on npu:0` + `InductorError: Device npu not supported`
- 根因: **venv 里 torch_npu 的 C 扩展不能正确工作**（venv python 下设备识别失败）。CI 的 A2 UT 用系统 python 跑
- 修复方向: A2-NPU-UT 不用 venv，直接用系统 python（和 CI 一致）；CPU-UT 保留 venv

### 问题 4: PR 描述缺陷（Files 列全空 + 空行）
- 根因: `gate_final_patch` 只在 gate PASSED 时写。131 个 UT 失败 → all_passed=False → 从不写
- fallback `step_patch` = 最后一步 (step-4) 的 step_target.patch = 只有 tracking file（1 个）
- `_extract_diff_files` 排除 tracking → `cumulative_files = []` → 所有行 Files 空
- 修复: `gate_final_patch` 缺失时从 git 重新生成 `git diff original_ascend_ref`

---

## 五、UT 隔离方案（_check_ut v2）— A2 实机验证成功

### 设计（8ff218b 已上线）

在 A2 NPU runner 上跑 CPU-UT 用例（mock 模式），完全隔离：

1. **每文件独立进程** — mock 污染免疫（test_batch_invariant.py 的 torch.library.Library monkeypatch 不可能泄漏到 test_gdn_layerwise_kv.py）
2. **假 npu-smi** — 临时目录放 `#!/bin/sh\nexit 1` 的 npu-smi 脚本，prepend 到子进程 PATH。vllm-ascend 的 tests/ut/conftest.py 检测 `npu-smi info` 失败 → 自动 mock torch_npu → CPU UT 用例以为自己在 CPU 上。不改 vllm-ascend 代码
3. **venv + numpy 1.26.4**（triton-ascend metadata 动态读）+ PYTHONPATH=ascend:vllm
4. **16 进程并行**（CPU-bound，不占 NPU），每文件 300s 超时

### A2 实机验证（2026-08-06，lwj-e2e 容器 @ 139.9.155.20）

环境：A2 910B3 NPU × 8，CANN 9.0.1（quay.nju.edu.cn/ascend/cann:9.0.1-910b-ubuntu22.04-py3.12），vllm 0351e9aa（VLLM_TARGET_DEVICE=empty），vllm-ascend bc5a79e（editable），torch 2.13.0

结果：
```
[pre_ci] ut: collected 166 CPU test files (per-file isolation)
[pre_ci] ut: creating venv at /tmp/ut_venv_xxx (numpy==1.26.4 from triton-ascend)
[pre_ci] ut: injected fake npu-smi at /tmp/ut_fake_bin_xxx (forces conftest mock path)
[pre_ci] ut: running 166 files (16 parallel, per-file isolation)...
[pre_ci] ut: 166/166 files clean, 0 failed
[pre_ci] ut: OK — all 166 files clean
```

- ✅ 假 npu-smi 骗过 conftest（PATH 注入后 npu-smi 返回 1 → mock 路径）
- ✅ 污染免疫：test_batch_invariant 10 passed；test_gdn_layerwise_kv 1 passed（之前同进程必失败）
- ✅ 166/166 全过，0 失败（4 分钟，16 并行）
- 166 文件（非 158）——vllm-ascend HEAD bc5a79e 比 d981aab12 多 8 个测试文件

对比 PR #13657 的 131 失败（14 污染 + 117 venv torch_npu 坏）→ **0 失败**

### 时间线澄清

- 8ff218b（Re-add CPU-UT gate）8/6 上午 push，**尚未被任何线上 main2main 调度 run 使用**（调度 = 每日 UTC 14:00 / 北京时间 22:00）
- 下一次调度 run（今晚 22:00 北京时间）才会第一次在线上跑 _check_ut v2

### 容器 setup 备忘（139.9.155.20）

```bash
# 容器 lwj-e2e 用 sleep infinity 重建（原容器 Cmd 为空启动即退出）
docker run -d --name lwj-e2e --device /dev/davinci0 ... -v /usr/local/dcmi:... quay.nju.edu.cn/ascend/cann:9.0.1-910b-ubuntu22.04-py3.12 sleep infinity
# 装依赖（镜像源）
pip config set global.index-url https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple
git config --global url."https://ghfast.top/https://github.com/".insteadOf "https://github.com/"
# vllm: VLLM_TARGET_DEVICE=empty uv pip install --system .（用 UV_DEFAULT_INDEX=清华）
# vllm-ascend: SOC_VERSION=ascend910b1 COMPILE_CUSTOM_KERNELS=0 uv pip install --system -e .
# main2main_flow: 复制到 /opt/main2main_flow，PYTHONPATH=/opt（symlink /opt/main2main_flow -> 实际目录）
```

---

## 关键文件

- `main2main_flow/flow.py` — initialize（MCP 配置）/ generate_final_post（PR 描述）/ _ai_analysis（动态 MCP）
- `main2main_flow/scripts/utils/pre_ci_check.py` — _check_ut
- `main2main_flow/scripts/utils/final_quality_gate.py` — 质量门禁
- `main2main_flow/scripts/agent/opencode_adapter.py` — opencode 运行 + [MCP] 日志
- `main2main_flow/agents/adapter/SKILL.md` — adapter 指令（MCP PRIMARY）
- `main2main_flow/agents/description-fill/SKILL.md` — unattributed 分析 role
- `main2main_flow/scripts/utils/vllm_report.py` — **已删除**（死代码）

## 相关链接

- PR #13515: https://github.com/vllm-project/vllm-ascend/pull/13515
- PR #13657: https://github.com/vllm-project/vllm-ascend/pull/13657
- main2main run 31026791091: https://github.com/vllm-project/vllm-ascend/actions/runs/31026791091
- vllm-report MCP 指南: https://github.com/vllm-ascend/vllm-report/blob/main/docs/mcp-usage-guide.md
- opencode 配置 docs: https://opencode.ai/docs/config
- schedule_main2main.yaml: https://github.com/vllm-project/vllm-ascend/blob/main/.github/workflows/schedule_main2main.yaml
