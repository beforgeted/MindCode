# MindCode CodeAgent

Python 实现的编码 Agent，支持上下文压缩、证据与长期记忆、事务化 Multi-Agent 编排，以及 Linux rootless Podman 沙箱。

## 能力概览

- **上下文管理**：预测式预算、图片与工具结果裁剪、Map/Reduce 压缩；按实际模型窗口组织请求，失败保留原历史。
- **任务编排**：Planner 拆解任务，隔离 Worker 并行执行，依赖门控、过期重跑与冲突处理；候选产物通过独立验收后才发布。
- **持久化与恢复**：保存 Attempt、任务轨迹和恢复依据；同 run 跨进程互斥，支持 Git 与非 Git 沙箱任务恢复。
- **执行隔离**：Podman 模式下文件读写、搜索与命令共用容器；默认断网、无宿主目录挂载，校验并销毁容器后才接收变更。
- **模型与费用**：七类角色独立模型配置、显式备用链、能力目录、成本记账与 Worker 软阈值路由；按模型和协议限频校准 token 估算。
- **长期记忆**：SQLite 存储与检索，候选抽取、去重、冲突治理，显式管理和可重建的 MEMORY.md 投影。
- **专项 Skill（功能分支）**：显式本地配置、Planner 选择与会话切换；权限交集在执行前复核，默认关闭。
- **社区生态（功能分支）**：加载标准 `SKILL.md` 包、读取冻结配套并显式授权 Python 脚本；社区 stdio MCP 的工具、固定资源和 Prompt 在现有 Podman 域调用，内容不扩大工具权限。

- **Knowledge（功能分支）**：默认关闭，显式检索当前候选的路径、Python 符号与文档；来源行号和内容版本支持再次校验。

P0–P9 主线、O1 观测、R1 角色/成本/窗口适配及 E10 保守校准一期已实现并合入 `main`。
当前 `dev/agent-ecosystem` 基于 Skill 检查点 `2416a72`，整合 MCP 一期 `1b8323a`；E1/E2 提交 `8cfa764`，Memory 提交 `ee76d67`，Fetch 提交 `84db854`，尚未合入主线。本轮完成 Knowledge 只读索引：主动查询路径/符号/文档、候选内容版本与引用校验；该实现保存在功能分支，默认关闭。人工Flash小样本对照已完成，未观察到稳定收益，并发现低输出限额下的缓存传输/域销毁缺口；实验保存在 `ablation/knowledge`，本轮已修复提前响应预算检查与执行域失效终止逻辑，详见[Knowledge验收](docs/testing/knowledge.md)。

当前分支分工：`main` 保存已发布基础功能，`dev/agent-ecosystem` 推进 Skill/MCP 生态，`codex/context-optimization-preview` 保留上下文优化与消融实验。已合并或被整合替代的旧开发、测试分支不再维护；历史实验记录仍留在本地忽略目录。

## 文档

公开文档集中在 `docs/`，按能力与测试主题整理。测试文档维护最新完整验收，阶段流水和个人现场记录留在本地。

| 文档 | 内容 |
|---|---|
| [系统架构](docs/architecture.md) | 主要模块、执行流程、存储职责和失败边界 |
| [Skill 使用与边界](docs/skills.md) | 配置、会话切换、Planner 选择、权限交集及失败行为 |
| [MCP 接入与边界](docs/mcp-tools.md) | 社区服务发现、目录固定、操作者授权和容器内调用 |
| [工具与能力测试](docs/testing/tools-and-skills.md) | 真实社区矩阵、双平台验收及尚未支持的能力 |
| [沙箱与恢复](docs/sandbox-and-recovery.md) | Podman、快照发布、受控下载、租约与崩溃恢复 |
| [模型与上下文](docs/model-and-context.md) | 角色路由、能力目录、成本、压缩、完整证据验收和 token 校准 |
| [最新测试总览与复现](docs/testing/README.md) | 验收版本、分环境结果、运行方式与证据口径 |
| [沙箱与恢复测试](docs/testing/sandbox-and-recovery.md) | 隔离、下载、交互、发布、强杀与恢复的统一判据 |
| [模型与上下文测试](docs/testing/model-and-context.md) | 模型路由、费用、窗口、压缩、验收与校准的统一判据 |
| [Knowledge 只读索引](docs/knowledge.md) / [测试](docs/testing/knowledge.md) | 当前候选内容检索、增量解析与来源引用校验 |
| [真实模型场景与 benchmark 使用](scenarios/README.md) | 显式启用的真实模型测试入口和任务判据 |

本轮 Knowledge 修复候选全量验收：Windows **922 passed、224 skipped**；独立 Ubuntu VM **1145 passed、1 skipped**，包括 **95 个真实 Podman 用例**，两端 Ruff/Pyright 通过。Knowledge 专项 **67/67**、11 个真实容器用例；原始冻结项目的六项检索对照 Top-5 **6/6**、引用 **27/27** 是此前版本结果，本轮未重跑。修复回归没有 WSL 或付费模型调用；原人工Flash消融结果与边界保留。Windows 148 个社区包完整匹配、1 包缺脚本，VM 149 个完整匹配；此输入边界仍保留。详情见[测试总览](docs/testing/README.md)与[Knowledge 验收](docs/testing/knowledge.md)。

基础主线代码提交 `0335117` 的历史完整验收：

| 环境 | pytest | 静态检查 |
|---|---|---|
| Windows | **658 passed、159 skipped** | Ruff / Pyright 通过 |
| 独立 Ubuntu VM | **816 passed、1 skipped**，含 **36 个真实 Podman 用例** | Ruff / Pyright 通过 |

专项用例包含在全量中，不另加总。这些结果验证控制流、真实容器与恢复门禁；尚无新一轮真实模型质量/费用对照结论。
Linux 测试只在独立 Ubuntu 虚拟机运行，**禁止使用 WSL Ubuntu 跑测试**。社区版本独立冻结、独立验收；主线历史数字单独保留。

## 快速开始

要求 Python 3.11 或以上。安装开发依赖并启动：

```bash
python -m pip install -e ".[dev]"
python -m codeagent.cli.app --workspace .
```

未配置 API key 时使用 StubLlmClient，不调用真实模型。接入 Anthropic 兼容端点时，由操作者配置实际凭据与模型；例如 PowerShell：

```powershell
$env:ANTHROPIC_API_KEY = "<你的 API key>"
$env:CODEAGENT_MODEL = "<端点支持的模型名称>"
python -m codeagent.cli.app --workspace .
```

不要把真实密钥写入跟踪文件。`.env` 已忽略。

## 交互与任务

直接输入目标走单 Agent；`/task <目标>` 走事务化 Multi-Agent：规划 → 隔离执行 → 候选集成 → 独立验收 → 发布。
Git 任务通过 CAS 推进真实 base；非 Git 沙箱任务通过快照候选与发布日志回写差异，不创建 Git 仓库。
含写入的沙箱任务必须配置 `CODEAGENT_VERIFY_CMD`；验收失败或无法判定时不发布变更。

| 命令 | 用途 |
|---|---|
| `/task <目标>` | 执行 Multi-Agent 任务 |
| `/skill list\|default\|名称` | 查看、切换专项能力或回退普通 Agent；切换新建上下文 |
| `/task --resume <mrun_id>` | 按持久状态恢复任务 |
| `/trajectory <mrun_id>` | 重建并查看任务轨迹 |
| `/context`、`/compact` | 查看上下文、请求压缩 |
| `/memory add\|list\|search\|show\|delete\|harvest` | 管理长期记忆 |
| `/metrics` | 查看当前会话指标 |
| `/clear`、`/quit` | 清空对话上下文、退出 |

Memory 示例：

```text
/memory add --type constraint --tag runtime "项目固定使用 Python 3.11"
/memory search Python
/memory list
```

Memory 在本地 `memory.db` 明文存储，`memory/MEMORY.md` 是可重建的人审投影。
会话结束或 `/memory harvest` 可抽取候选，经 Judge 治理后写入；无真实模型时 Stub 保守地不写入。

## 模型与预算配置

可配置角色：`PLANNER`、`WORKER`、`LOCAL_VERIFIER`、`GLOBAL_VERIFIER`、`JUDGE`、`COMPACT_MAP`、`COMPACT_REDUCE`。

```powershell
$env:CODEAGENT_MODEL_PLANNER = "<规划模型>"
$env:CODEAGENT_MODEL_WORKER = "<执行模型>"
$env:CODEAGENT_MODEL_FALLBACK_PLANNER = "<备用模型一>;<备用模型二>"
```

不配置备用链时没有自动 fallback；限流、超时或暂时不可用才触发显式备用链。
能力目录按实际 Provider/模型声明窗口、输出、工具、图片与温度支持，不兼容请求明确拒绝或跳过备用候选。

| 配置 | 用途 |
|---|---|
| `CODEAGENT_MODEL_CAPABILITIES` | 本地模型能力 JSON 路径 |
| `CODEAGENT_SKILLS_CONFIG` | 显式本地 Skill 配置 JSON 路径；缺省关闭 |
| `CODEAGENT_MODEL_PRICES` | 本地价格 JSON 路径，价格由操作者核对 |
| `CODEAGENT_WORKER_COST_THRESHOLD_USD`、`CODEAGENT_MODEL_ECONOMY_WORKER` | 成对启用单 run Worker 软成本阈值路由 |
| `CODEAGENT_TOKEN_CALIBRATION=0` | 关闭默认启用的保守校准 |
| `CODEAGENT_TOKEN_COUNT_TIMEOUT_SECONDS` | 单次计数超时，默认 2 秒 |
| `CODEAGENT_TOKEN_COUNT_INTERVAL_SECONDS` | 同类别采样间隔，默认 60 秒 |
| `CODEAGENT_TOKEN_COUNT_MAX_CALLS` | 每会话计数接口尝试上限，默认 16 次 |

完整配置与局限见[模型与上下文](docs/model-and-context.md)。软阈值不等于账单硬上限，未知费用不能当作零。

## Podman 沙箱

默认 `local` 后端在本机执行。显式选择 `podman` 需要 Linux rootless Podman、cgroup v2 及 CPU/memory/pids 控制器。
操作者须预先准备包含 Python、shell 和任务依赖的受信镜像，配置完整 SHA256 镜像 ID；后端不会自动拉取镜像或退回本机执行。

以下命令仅在独立 Ubuntu 虚拟机执行：

```bash
export CODEAGENT_EXECUTION_BACKEND=podman
export CODEAGENT_SANDBOX_IMAGE='<已安装的完整 SHA256 镜像 ID>'
export CODEAGENT_VERIFY_CMD='python -m pytest -q'
python -m codeagent.cli.app --workspace /path/to/project
```

容器保持断网。可通过精确主机白名单配置受控 HTTPS 下载；它不会开放容器网络或在线包管理。
运行机制、发布条件和恢复边界见[沙箱与恢复](docs/sandbox-and-recovery.md)。

## 开发与测试

```bash
python -m pytest -q
python -m ruff check .
python -m pyright
```

普通 pytest 使用确定性模型替身；真实模型场景须显式启动，运行方式见[场景套件](scenarios/README.md)。
真实 Podman 测试需要额外设置 `MINDCODE_PODMAN_TEST_IMAGE`；它不会自动把应用后端切换为沙箱。
分环境运行和复现前提见[测试总览](docs/testing/README.md)。

个人连接信息、内部草稿、原始实验流水与历史现场记录集中在 `.private-docs/`，整个目录由 `.gitignore` 排除。
原始日志和测试产物保存在已忽略的 `.codeagent/validation/`；公开说明只包含能力、判据和去除个人信息后的结果。
