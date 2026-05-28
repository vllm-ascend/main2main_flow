# Main2Main Flow — 使用指南

## 背景与目标

vllm-ascend 是 vLLM 的昇腾（Ascend NPU）硬件适配插件，其代码以 vLLM 的某个特定 commit 为基础，通过继承和覆写 vLLM 内部接口来实现昇腾硬件支持。随着 vLLM 上游 main 分支持续演进，接口签名、内部类结构、配置项等会不断变化，vllm-ascend 必须跟随这些变化做出相应适配，否则就会出现运行时错误甚至编译失败。

这个同步过程被称为 **main2main 升级**。每次升级本质上是：

1. 找出 vLLM 从"当前已同步版本"到"目标版本"之间新增的所有 commit
2. 分析这些 commit 改动了哪些接口或内部实现
3. 在 vllm-ascend 中做出对应修改，确保适配层与新版 vLLM 保持兼容
4. 跑 e2e CI 验证修改是否正确
5. 通过后提交 PR

过去这个过程完全靠人工完成，耗时且容易遗漏。**Main2Main Flow** 将其自动化：它由确定性脚本（commit 检测、步骤规划、版本引用更新、CI 校验）与 AI Agent（代码分析、适配、审查）协同驱动，全流程无需人工介入即可完成一次 vLLM 版本升级。

---

## 快速开始

### 前置条件

- Python 3.10–3.13
- 已安装 [crewAI](https://github.com/joaomdmoura/crewAI)（`pip install crewai`）
- 本地已有 vllm 和 vllm-ascend 的 git 仓库，或可以访问 GitHub 进行 clone
- 如需运行 e2e 测试：目标机器上有昇腾 NPU 设备，并配置好 Docker 容器环境
- 如需自动推 PR：已安装并登录 `gh`（GitHub CLI）

### 安装

```bash
# 进入项目目录
cd main2main_flow

# 安装 crewAI 和项目依赖
pip install crewai
pip install -e .
```

安装完成后，`kickoff` 命令会被注册为可执行入口。

### 运行方式

**方式一：直接指定本地仓库路径**

```bash
kickoff \
  --vllm-path /path/to/vllm \
  --vllm-ascend-path /path/to/vllm-ascend
```

这是最常见的用法。两个仓库必须是已经 clone 好的本地 git 仓库，vllm 仓库的 HEAD 即为目标版本。

**方式二：指定升级目标 commit**

```bash
kickoff \
  --vllm-path /path/to/vllm \
  --vllm-ascend-path /path/to/vllm-ascend \
  --target-commit a1b2c3d4e5f6...  # 40 位 SHA
```

不传 `--target-commit` 时，默认跑到 vllm 仓库当前 HEAD。如果你希望只同步到某个中间版本而不是最新 HEAD，可以手动指定。

**方式三：传 GitHub URL（自动 clone）**

```bash
kickoff \
  --vllm-path https://github.com/vllm-project/vllm.git \
  --vllm-ascend-path https://github.com/vllm-project/vllm-ascend.git
```

如果本地没有仓库，可以直接传 GitHub URL。Flow 会在启动时自动 clone 到 `workspace/repos/` 目录下，后续操作均在 clone 出来的副本中进行，不影响任何已有目录。

**方式四：使用环境变量（适合 CI 脚本）**

```bash
export VLLM_PATH=/path/to/vllm
export VLLM_ASCEND_PATH=/path/to/vllm-ascend
export PUSH_TO_GITHUB=true
export GITHUB_REPO=vllm-project/vllm-ascend

kickoff
```

所有 CLI 参数都有对应的环境变量，适合在 CI/CD 流水线中使用。

### 跳过 e2e 测试（仅验证 AI 适配结果）

```bash
SKIP_E2E_TEST=true kickoff \
  --vllm-path /path/to/vllm \
  --vllm-ascend-path /path/to/vllm-ascend
```

跳过 e2e 测试时，所有步骤在 AI 适配和 pre-CI 校验通过后即视为完成，不会真正执行 NPU 测试。适用于只想查看 AI 生成的适配代码、或测试环境暂不可用的场景。

### 环境变量完整说明

| 变量 | 说明 | 默认值 |
|---|---|---|
| `VLLM_PATH` | vllm 本地路径或 GitHub URL。CLI `--vllm-path` 优先级更高 | `workspace/repos/vllm` |
| `VLLM_ASCEND_PATH` | vllm-ascend 本地路径或 GitHub URL。CLI `--vllm-ascend-path` 优先级更高 | `workspace/repos/vllm-ascend` |
| `VLLM_TARGET_COMMIT` | 目标 vllm commit SHA（40 位）。不设置则以 vllm HEAD 为目标 | vllm HEAD |
| `SKIP_E2E_TEST` | 设为 `true` 跳过所有 e2e NPU 测试，所有步骤直接视为通过 | `false` |
| `PUSH_TO_GITHUB` | 设为 `true` 在全部步骤成功后自动创建 PR | `false` |
| `GITHUB_REPO` | PR 目标仓库，格式 `owner/name`（如 `vllm-project/vllm-ascend`） | — |
| `MAIN2MAIN_REMOTE_HOST` | e2e 测试远程机器的 SSH 地址（如 `root@192.168.1.10`）。不设置则在本机执行 | — |
| `MAIN2MAIN_REMOTE_CONTAINER` | 远程机器上已存在的 Docker 容器名，测试命令将通过 `docker exec` 在其中运行 | — |
| `CI_RESULTS_DIR` | `generate_final_post` 读取 CI 结果的目录（一般不需要手动设置） | `workspace/ci_results` |
| `STEPS_DIR` | `generate_final_post` 读取步骤总结的目录（一般不需要手动设置） | `workspace/steps` |

---

## 工作流总览

整个 Flow 由 6 个节点组成，核心是一个针对每个步骤的"适配 → 测试 → 重试"循环。

![Flow 结构图](images/flow.png)


Flow 中的节点通过字符串信号（`HasCommit`、`StepCompleted` 等）传递控制权，crewAI 的 `@router` 和 `@listen` 装饰器决定哪个节点监听哪个信号。这些信号名在 `utils.py` 中定义为常量，在 `main.py` 的各节点返回值中使用。

---

## 各步骤详解

### Step 1 — `initialize`

**触发条件**：Flow 入口，由 `@start` 装饰，是整个工作流第一个执行的节点。

**核心功能**

初始化阶段做两件事：清理工作区、规范化路径。每次运行都会彻底删除并重建 `workspace/` 目录，确保本次运行的所有产物与上次运行完全隔离，不会因为残留文件干扰后续步骤的判断。

路径规范化逻辑如下：优先使用 CLI 参数，其次读取对应环境变量，最后使用默认值（`workspace/repos/<name>`）。如果最终得到的路径是一个 GitHub URL（以 `https://` 或 `git@` 开头），则自动执行 `git clone` 将仓库下载到 `workspace/repos/` 目录，并将本地路径记录到 state 中供后续节点使用。

**输入**

- CLI 参数 `--vllm-path`、`--vllm-ascend-path`、`--target-commit`（三者均可选）
- 或对应环境变量 `VLLM_PATH`、`VLLM_ASCEND_PATH`、`VLLM_TARGET_COMMIT`
- 若均未设置，`vllm_path` 和 `vllm_ascend_path` 默认为 `workspace/repos/vllm` 和 `workspace/repos/vllm-ascend`

**输出**

- 空的 `workspace/` 目录（旧目录已被删除）
- Flow state 中写入以下字段，供后续所有节点读取：
  - `state.vllm_path`：vllm 仓库的本地绝对路径
  - `state.vllm_ascend_path`：vllm-ascend 仓库的本地绝对路径
  - `state.target_commit`：目标 commit SHA（可能为空，表示以 vllm HEAD 为目标）
  - `state.test_log_dir`：测试日志输出目录，默认为 `workspace/test-logs`

---

### Step 2 — `analyze_commit_and_plan_step`

**触发条件**：`initialize` 完成后，由 `@router` 装饰，执行完毕后根据结果路由到 `HasCommit` 或 `HasNoCommit`。

**核心功能**

这一步回答两个问题：

1. **需要同步多少内容？** — 找出 vllm-ascend 当前已同步到哪个 vllm commit，与目标 commit 对比，确定需要同步的范围。
2. **怎么分批同步？** — 如果范围很大（跨越几十个 commit），一次性全部适配风险太高，需要拆分成若干大小适中的步骤逐步推进。

#### 子步骤 2.1 — 检测 commit 范围（`detect_commits.py`）

vllm-ascend 在 `docs/source/conf.py` 文件的 `myst_substitutions` 字典中维护了两个字段：

- `main_vllm_commit`：记录当前 vllm-ascend 已经适配并验证过的 vllm commit SHA（即 base commit）
- `main_vllm_tag`：对应的 vllm release tag（如 `v0.9.1`），用于后续 `vllm_version_is()` 版本 guard 的正确性校验

检测逻辑：读取 `conf.py` 中的 `main_vllm_commit` 作为 base，读取 vllm 仓库 HEAD（或 `target_commit`）作为 target。如果两者相同，说明已经同步到最新，返回 `HasNoCommit` 信号，流程直接结束；否则返回 `HasCommit`，继续规划步骤。

检测结果写入 `workspace/detect.json`：

```json
{
  "base_commit": "a1b2c3d4e5f6...",
  "target_commit": "f6e5d4c3b2a1...",
  "compat_tag": "v0.9.1"
}
```

#### 子步骤 2.2 — 规划适配步骤（`plan_steps.py`）

将 base 到 target 之间的所有 commit 拆分为若干"步骤"（step）。拆分的目的是控制每一步的改动量，避免单次适配涉及过多文件变化导致 AI 分析不准或 CI 定位困难。

**分组算法**：

1. `git log --reverse base..target` 按时间正序列出所有 commit
2. 对每个 commit，使用 `git diff-tree --numstat` 统计各文件的增删行数
3. 文件按路径分类：`vllm/` 目录下的源文件计入 `vllm_changed_lines`；`pyproject.toml`、`requirements/` 等依赖文件标记为 `requirements` 类型；其余（docs、tests、CI 脚本等）忽略不计
4. 分组规则（按优先级）：
   - **依赖变更单独成步**：含有 `requirements` 类文件的 commit 必须单独成步，因为依赖更新可能影响构建环境，需要隔离验证
   - **超大 commit 单独成步**：单个 commit 的 `vllm_changed_lines > 1000` 时单独成步，避免分析时上下文过长
   - **累积分组**：其余 commit 累积到当前步，当累积的 `vllm_changed_lines` 超过 1000 行，或 commit 数量超过上限时，将当前批次封装为一步，重新开始累积

这样规划出的每一步变更量适中，通常涵盖 1–10 个 commit，vllm 源码变更行数在 1000 行以内。

**输出**：
- `workspace/steps.json`：完整的步骤计划，每个步骤记录其包含的 commit 列表、commit 范围（`start_commit`、`end_commit`）、涉及的文件和总变更行数：

  ```json
  {
    "base_commit": "a1b2c3...",
    "target_commit": "f6e5d4...",
    "total_commits": 24,
    "steps": [
      {
        "id": "step-1",
        "index": 1,
        "start_commit": "a1b2c3...",
        "end_commit": "d4e5f6...",
        "commits": [
          {"sha": "b2c3d4...", "subject": "feat: add new attention backend option"},
          {"sha": "d4e5f6...", "subject": "refactor: split platform config"}
        ],
        "commit_count": 2,
        "vllm_changed_lines": 340,
        "total_changed_lines": 412,
        "files_changed": ["vllm/attention/backends/flash_attn.py", "vllm/config.py"]
      }
    ]
  }
  ```

- `workspace/steps/` 目录：空目录，供后续每个步骤写入各自的产物
- `state.steps`：步骤列表写入 flow state，驱动后续循环迭代
- `state.release_tag`：兼容版本 tag，写入 flow state，在整个适配过程中作为 `vllm_version_is()` 的版本基准

---

### Step 3 — `ai_analysis`

**触发条件**：监听 `HasCommit`、`StepCompleted`、`StepRetryNeeded` 三个信号（`@listen(or_(...))`）。每当有新步骤需要适配（无论是第一次还是测试失败后重试）都会触发此节点。

**核心功能**

这是整个工作流最核心的节点，由两部分组成：确定性的准备工作（不依赖 AI），以及 AI 驱动的代码适配循环。

#### 准备阶段（确定性操作）

**1. checkout vllm 到本步目标 commit**

在开始分析之前，将 vllm 仓库 `git checkout` 到本步的 `end_commit`，确保后续 AI agent 读取 vllm 源码时看到的是与 upstream patch 对应的版本，而不是更新或更旧的代码。

**2. 生成 upstream patch**

使用 `git diff start_commit..end_commit` 生成本步的上游变更 patch，写入 `workspace/steps/<step-id>/upstream.patch`。同时生成变更文件列表 `changed-files.txt`。这个 patch 是后续 AI 分析的核心输入——agent 通过它了解上游改动了什么。

**3. 更新 commit 引用（`update_commit_reference.py`）**

vllm-ascend 在多个文件（`conf.py` 以及可能的其他配置文件）中硬编码了当前对齐的 vllm commit SHA。在每一步适配开始时，需要将这些旧 SHA 替换为本步的目标 SHA，以保持文档和配置的一致性。

具体做法：扫描 vllm-ascend 仓库所有被 git 追踪的文件（`git ls-files`），将文件内容中出现的旧 commit SHA 批量替换为新 SHA。这是纯文本替换，不依赖文件格式或路径，对二进制文件自动跳过。首轮（mode=adapt）执行一次，重试轮次（mode=fix）跳过（因为引用已经在首轮更新过了）。

#### AI 适配循环（最多重试 3 次）

准备工作完成后，进入 AI 适配循环。每次循环调用一次 **AdapterCrew**，再执行一次 **pre-CI 校验**。如果 pre-CI 通过则退出循环，否则将校验错误日志反馈给下一轮 crew，以 `fix` 模式重新适配，最多 3 次。

**AdapterCrew** 是一个由 4 个 AI agent 构成的层级式工作组（`Process.hierarchical`），各 agent 职责如下：

| Agent | 可用工具 | 职责描述 |
|---|---|---|
| `patch_analyzer` | `FileReadTool` | 读取 upstream.patch 和 changed-files.txt，分析哪些上游子系统发生了变化（attention backend、worker 生命周期、platform 接口、config 结构等），并查阅 vllm-ascend 源码，确定哪些文件需要适配、需要做什么变更。在 fix 模式下读取错误日志，诊断根因 |
| `analyzer_QA` | `FileReadTool` | 审查 `patch_analyzer` 的分析结论，逐一核验：所有变更文件是否都被覆盖、File Mapping Table 是否被正确查阅、"无需适配"的结论是否有充分理由。分析不完整时会打回要求重做，而不是放行 |
| `code_adapter` | `FileReadTool`, `FileWriterTool`, `ShellCommandTool` | 根据分析结果直接修改 vllm-ascend 源文件。遵循严格的修改规范：只改 vllm-ascend（不改 vllm）、用 `git add <file>` 提交（不用 `git add .`）、临时文件只写 `/tmp/main2main/` 下、版本兼容分支必须用 `vllm_version_is()` 而非 `hasattr` 或 try/except |
| `code_reviewer` | `FileReadTool`, `ShellCommandTool` | 执行 `git diff HEAD` 查看实际变更，与 `patch_analyzer` 的计划对照核验：每个需要改的文件是否都改了、方法签名是否与上游完全一致、`vllm_version_is()` 的版本号是否正确、所有版本分支是否具有相同的函数签名、有没有漏改的调用点 |

**两种运行模式**

- **`adapt` 模式**（首次或新步骤）：从 upstream patch 出发，分析上游改动并将其适配到 vllm-ascend
- **`fix` 模式**（pre-CI 或 e2e 测试失败后）：从错误日志出发，诊断根因并修复，不做新的 upstream 适配

**每次 crew 完成后执行 pre-CI 校验（`pre_ci_check.py`）**

pre-CI 校验是 AI 适配环节的"快速门"，在真正跑 NPU 测试之前用确定性规则拦截常见错误：

- **版本字符串检查**：扫描本次 `git diff HEAD` 中新增的行，找出所有 `vllm_version_is("...")` 调用，检查其中的版本号是否与 `release_tag` 完全一致。注意：检查范围是仅限新增行，不影响历史遗留的版本 guard
- **临时文件检查**：检查 vllm-ascend 工作区是否存在 `.patch`、`.log`、`.jsonl`、`vllm_changes.md` 等临时文件。这类文件若被误提交会污染仓库

校验结果写入 `workspace/steps/<step-id>/pre_ci_check-round-<n>.json`：

```json
{
  "all_passed": true,
  "checks": [
    {
      "name": "version_strings",
      "passed": true,
      "detail": "2 new vllm_version_is() calls all use v0.9.1"
    },
    {
      "name": "temp_files",
      "passed": true,
      "detail": "no temp files in repo"
    }
  ]
}
```

**ai_analysis 阶段的全部输出**（每步）：

| 文件 | 内容 |
|---|---|
| `workspace/steps/<step-id>/upstream.patch` | 本步 vllm 上游变更的完整 diff |
| `workspace/steps/<step-id>/changed-files.txt` | 本步变更的 vllm 文件名列表（每行一个） |
| `workspace/steps/<step-id>/pre_ci_check-round-<n>.json` | 第 n 次 crew 尝试后的 pre-CI 校验结果 |
| `workspace/steps/<step-id>/summary.md` | AI 生成的本步适配总结（由 `code_reviewer` 最终输出） |
| `workspace/steps/<step-id>/adaptation.patch` | vllm-ascend 本步全量变更（`git diff HEAD`） |

同时更新 flow state：`state.cur_vllm_commit`、`state.cur_ascend_commit`（当前 HEAD）、`state.cur_patch_path`（adaptation.patch 路径），供 `run_e2e_test` 使用。

---

### Step 4 — `run_e2e_test`

**触发条件**：`ai_analysis` 完成后（`@router`），根据测试结果路由到四个出口之一。

**核心功能**

在真实的昇腾 NPU 环境中搭建测试环境，执行 e2e CI 测试套件，判断本步 AI 适配结果是否正确可用。支持本地执行和通过 SSH 在远程机器上执行两种模式。

#### 环境搭建

无论本地还是远程，环境搭建流程相同：

1. **vllm 仓库**：clone（若不存在）或 fetch（若已存在），checkout 到 `cur_vllm_commit`，然后以 `VLLM_TARGET_DEVICE=empty` 运行 `pip install -e .`（empty device 模式安装依赖但不编译 GPU 扩展，速度更快）
2. **vllm-ascend 仓库**：同样 clone 或 fetch，checkout 到 `cur_ascend_commit`
3. **应用 adaptation.patch**：若存在 patch 文件，通过 `git am` 应用到 vllm-ascend，使其包含本步 AI 生成的全部适配代码
4. **安装 vllm-ascend 依赖**：运行 `pip install -r requirements-dev.txt`；仅在 fresh clone 时追加 `pip install -e .`（已存在的仓库跳过 editable install 以节省时间）

远程执行时，上述步骤被打包成一个 shell 脚本，通过 `ssh <host> docker exec <container> sh -c "..."` 在远端容器中执行，本地不需要有 NPU 环境。

#### 测试套件调度

支持并发运行多个测试套件，按 NPU 卡数进行资源调度：

| 套件名 | 所需卡数 |
|---|---|
| `e2e-singlecard-light` | 1 卡 |
| `e2e-2card-light` | 2 卡 |
| `e2e-4card-light` | 4 卡 |
| `e2e-singlecard` | 1 卡 |
| `e2e-multicard-2-cards` | 2 卡 |
| `e2e-multicard-4-cards` | 4 卡 |

调度算法（贪心 first-fit decreasing bin-packing）：将套件按卡数降序排列，尽量将多个套件塞进同一轮次（round）同时运行，充分利用总卡数。每个套件分配到独立的设备 ID 范围，通过 `ASCEND_RT_VISIBLE_DEVICES` 环境变量隔离，避免竞争。不同轮次串行执行（等上一轮所有套件结束才开始下一轮）。

每个套件的测试结果由 `ci_log_summary.py` 解析日志并分类：
- `passed`：所有用例通过
- `env_flake_pass`：有失败用例，但全部被识别为环境抖动（env flake），视为通过
- `failed`：存在代码 bug 导致的失败（`code_bugs_count > 0`）
- `summary_error`：日志解析失败，无法判断

只要任何一个套件报告 `failed`，整轮测试即为失败；所有套件均为 `passed` 或 `env_flake_pass` 时，测试视为通过（`can_commit=true`）。

**输出文件**（每步每轮次）：

| 文件 | 内容 |
|---|---|
| `workspace/steps/<step-id>/tests/round-<n>-<suite>.log` | 测试套件的完整原始输出日志 |
| `workspace/steps/<step-id>/tests/round-<n>-<suite>-summary.json` | 解析后的结构化错误摘要，LLM 友好格式，供 fix 模式使用 |
| `workspace/steps/<step-id>/tests/round-<n>-result.json` | 本轮所有套件的汇总结果 |

**路由逻辑**

| 条件 | 路由信号 | 后续行为 |
|---|---|---|
| `can_commit=true`，当前步不是最后一步 | `StepCompleted` | `current_step++`，以 adapt 模式进入下一步的 `ai_analysis` |
| `can_commit=true`，当前步是最后一步 | `UpgradeCompleted` | 进入 `generate_final_post` |
| `can_commit=false`，`retry_count < 3` | `StepRetryNeeded` | `retry_count++`，`test_errors` 写入失败日志路径，以 fix 模式重新进入 `ai_analysis` |
| `can_commit=false`，`retry_count >= 3` | `UpgradeFailed` | 放弃本步，进入 `generate_final_post` |

设置 `SKIP_E2E_TEST=true` 时，此节点不执行任何测试，直接按照"通过"逻辑路由。

---

### Step 5 — `generate_final_post`

**触发条件**：监听 `UpgradeCompleted` 或 `UpgradeFailed`，无论升级成功还是中途失败都会执行。

**核心功能**

由 **SummaryCrew** 驱动，读取本次运行所有步骤的产物，生成一份完整的、适合人类阅读的 Markdown 最终报告。

SummaryCrew 使用两个自定义工具：`ListFilesTool`（列出目录下的文件）和 `ReadFileTool`（读取文件内容），让 `summary_reporter` agent 自主浏览步骤目录并提取关键信息。

agent 按照以下流程生成报告：

1. 读取 `ci_results_dir` 中的 CI 日志（`*.log`）和结果 JSON（`*-result.json`），确定每一步的 CI 结论
2. 读取 `steps_dir` 中各步骤的 `summary.md`，了解每步做了哪些适配
3. 检查每步是否有 patch 文件：有 patch 文件说明做了代码改动，没有则说明只更新了 commit 引用（no-op 步骤）
4. 判断整体状态：所有步骤都完成且到达目标 commit → `completed`；中途被迫停止（非因 session 或费用限制）→ `partial`
5. 按固定 Markdown 模板输出报告

**输出**：`output/final_summary.md`，内容包含：

- 整体状态（`completed` / `partial`）与上游 commit 范围
- 各步骤结果表格（步骤 ID、upstream 范围、vllm-ascend commit、CI 结果、适配描述）
- 变更摘要（commit 引用从哪更新到哪、改动了哪些 vllm-ascend 模块、添加了哪些版本兼容 guard）
- CI 验证结论（通过的步骤、被判定为 env flake 的用例）
- （状态为 partial 时）停止原因、未解决的失败、保存的 patch 路径、回滚后的仓库状态
- （有后续行动时）下一步建议

---

### Step 6 — `push_to_github`

**触发条件**：`UpgradeCompleted` 和 `generate_final_post` 同时完成（`@listen(and_(...))`）。因此只有在升级完全成功、且报告已生成时才会执行；`UpgradeFailed` 不会触发此步骤。

**核心功能**

将本次升级的适配代码推送到 GitHub 并自动创建 Pull Request。这一步是可选的，需要显式设置 `PUSH_TO_GITHUB=true` 才会执行，否则打印提示后直接跳过，方便在不自动推送的场景下手动审查代码后再决定是否推。

**执行流程**：

1. **找到最终 patch 文件**：扫描 `workspace/steps/` 目录，找到编号最大的 `*.patch` 文件，即最后一步的 `adaptation.patch`，它包含整个升级过程的全量代码改动
2. **创建新分支**：在 vllm-ascend 仓库中 `git checkout -b update/main2main-<timestamp>`，时间戳确保分支名唯一
3. **应用 patch**：`git apply <patch>`，将适配代码写入工作区
4. **提交**：`git add -A && git commit -s -m "main2main: sync vllm upstream (<timestamp>)"`，`-s` 添加 Signed-off-by
5. **推送**：`git push origin <branch>`
6. **创建 PR**：`gh pr create --title <commit-msg> --body <final_summary.md>`，以 `final_summary.md` 的内容作为 PR 描述

**输出**：GitHub PR URL（打印到标准输出并作为节点返回值）

---

## 工作区目录结构

每次运行都会在项目根目录下创建（或覆盖）`workspace/`（代码中实际路径为 `test/`，由 `WORKSPACE_DIR` 常量控制）。运行完成后目录结构如下：

```
workspace/
├── detect.json                           # 检测结果：base/target commit 和 compat_tag
├── steps.json                            # 完整步骤计划：所有步骤的 commit 范围和变更统计
├── repos/                                # 自动 clone 的仓库（仅在传入 GitHub URL 时存在）
│   ├── vllm/
│   └── vllm-ascend/
└── steps/
    ├── step-1/
    │   ├── upstream.patch                # 本步 vllm 上游 diff（git diff start..end）
    │   ├── changed-files.txt             # 本步变更的 vllm 文件路径列表
    │   ├── pre_ci_check-round-1.json     # 第 1 次 crew 尝试后的 pre-CI 校验结果
    │   ├── pre_ci_check-round-2.json     # 第 2 次尝试后的校验结果（如有重试）
    │   ├── summary.md                    # AI 生成的本步适配总结
    │   ├── adaptation.patch              # vllm-ascend 本步实际变更（git diff HEAD）
    │   └── tests/
    │       ├── round-1-e2e-singlecard-light.log          # 套件原始日志
    │       ├── round-1-e2e-singlecard-light-summary.json # 结构化错误摘要
    │       └── round-1-result.json                       # 本轮汇总（can_commit、ci_result 等）
    ├── step-2/
    │   └── ...
    └── step-N/
        └── ...

output/
└── final_summary.md                      # 最终升级报告（SummaryCrew 生成）
```

每次运行开始时 `workspace/` 会被完全清空，因此如果需要保留上次运行的产物，请在运行前手动备份。

---

## AI Agent 参考文档

AdapterCrew 的 agent 在分析和适配过程中会参考项目内置的参考文档，这些文档编码了 vllm-ascend 适配工作的领域知识，是 AI 分析质量的重要保障。文档位于 `src/main2main_flow/reference/`：

| 文件 | 主要内容 |
|---|---|
| `adapt-guide.md` | **Key Areas 表**：vLLM 内部子系统（attention backend、worker、platform、config 等）与 vllm-ascend 实现之间的映射关系；**File Mapping 表**：上游 vllm 文件路径 → 对应需要关注的 vllm-ascend 文件；**适配步骤指引**：patch_analyzer 和 code_adapter 执行任务时的操作规范 |
| `diagnosis-guide.md` | 常见 CI 错误类型的诊断流程：签名不匹配、import 错误、版本 guard 问题、临时文件残留等，以及对应的修复模式 |
| `error-pattern-examples.md` | 具体错误案例和对应修复代码示例，帮助 agent 快速识别已知错误模式并套用正确的修复方法 |

这些文档随着项目演进应当持续维护，当出现新的适配错误类型或发现 agent 存在分析盲区时，应将相关经验沉淀到对应文档中，以提升后续运行的适配准确率。
