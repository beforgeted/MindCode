# MindCode CodeAgent

Python 实现的编码 Agent。设计文档见仓库根目录的四份 md，落地方案与分期见
[`MindCode_实现设计_V1.md`](MindCode_实现设计_V1.md)。

当前落地范围：**P0–P9**（单 Agent + Multi-Agent 事务化编排 + 编排持久化/恢复与共享 Memory
+ 工具执行安全 + Attempt 级幂等崩溃恢复 + 小规模质量 benchmark）。
收尾接线 **A1 / A3 / C7 / A2** 已实现：会话计量、交互审批、Worker 记忆候选抽取与延后外部动作恢复。
优化 **O1** 已实现：角色/模型调用归因、Attempt 状态历史与任务轨迹 JSON 导出。
优化 **R1 第一期** 已实现：角色模型配置、Provider 注册与受控 fallback；R1二期显式定价、持久化成本与Worker阈值降级也已实现。
**B4/B5 沙箱**已接入 Git / 非 Git 普通交互、`/task` Worker 与独立验收：Linux rootless Podman、无宿主目录挂载、
离线执行与受校验快照回传。B5 一期新增受控 HTTPS 文件下载，容器仍断网。C6 同 run 跨进程恢复互斥、
非 Git任务持久化恢复与staging崩溃回收也已完成。最新独立 Ubuntu VM **647 passed、1 skipped**，30 个真容器用例通过；Windows **495 passed、153 skipped**，两端 Ruff/Pyright 通过。
真实 deepseek-flash 历史11场景和非Git任务正/负例通过是此前阶段的模型回归；本期恢复测试用Stub固定
模型步骤，容器与进程强杀是真实操作，下载回归使用真实HTTPS。
详见 [`LINUX_SANDBOX_ACCEPTANCE.md`](LINUX_SANDBOX_ACCEPTANCE.md)。

## 快速开始

```bash
conda activate mindcode
pip install -e ".[dev]"

# 不设 API key 也能跑（走 StubLlmClient，不真的调模型）
python -m codeagent.cli.app --workspace .

# 接真实模型
export ANTHROPIC_API_KEY=sk-ant-...
python -m codeagent.cli.app --workspace /path/to/repo
```

REPL 命令：`/context` `/compact` `/memory add|list|search|show|delete|harvest`
`/task <目标>` `/trajectory <mrun_id>` `/clear` `/metrics` `/quit`。`/task` 走事务化 Multi-Agent：规划 → 并行 Worker
（各自 git worktree 隔离，集成进 candidate 而非真实 base）→ 依赖门控/过期重跑/Integrator 兜底
自愈冲突 → **产物级全局验收** → 通过才 **CAS 原子推进真实 base**，否则整个 Attempt 丢弃、从
起点重开（用户只看到"任务完成/未完成"，不接触 git 冲突）。非 git 环境回退只读并行+写串行。
`/task --resume <mrun_id>` 恢复：Attempt 级幂等恢复——已 promote 的识别为完成不重推，
PROMOTING 崩溃窗口按真实 base HEAD 判定，其余状态回收孤儿后从持久 original_base 重开（P8）。
代码合并后，resume 还会处理原 Attempt 中未完成的外部动作，恢复规则见下文。
可选 `CODEAGENT_VERIFY_CMD` 指定确定性验收命令，在独立 validation worktree 运行。

Memory 示例：

```text
/memory add --type constraint --tag runtime "项目固定使用 Python 3.11"
/memory search Python
/memory list
/memory show mem_xxx
/memory delete mem_xxx
```

Memory 以本地明文保存在项目 `memory.db` 中，`memory/MEMORY.md` 只是可重建的
人审投影。除用户显式 `/memory add` 外，P4 起会在 Session End（或 `/memory harvest`）
自动从会话事件里抽取候选、经 LLM Judge 判断后治理写入；不设 API key 时 Judge 走
StubLlmClient，会保守地不写入。

```bash
pytest              # 全部测试
ruff check .        # lint
pyright             # 类型
```

## 已实现

| 能力 | 位置 |
|---|---|
| 唯一 TokenEstimator（启发式 + 精确计数校准） | `context/token_estimator.py` |
| 全配置化阈值（soft/hard/target + 预测加项） | `context/profile.py` |
| 预测式压缩触发 | `context/budget.py` |
| ContextManager 统一入口 | `context/manager.py` |
| 图片 payload 裁剪（保留描述） | `context/prune/image_pruner.py` |
| tool 结果 HOT/WARM/COLD 降级 | `context/prune/tool_result_offloader.py` |
| RawEventStore（JSONL，队列单写者） | `evidence/jsonl_event_store.py` |
| ArtifactStore（流式写入） | `evidence/artifact_store.py` |
| tool 边界有界化 | `tool/normalizer.py` |
| tool 并发 + 协议完整性保证 | `tool/execution_manager.py` |
| ReAct 主循环 | `runtime/react_engine.py` |
| P2 turn-atomic Map/Reduce + TaskCheckpoint | `context/compact/` |
| PROJECT SQLite Durable Memory + FTS/LIKE | `memory/sqlite_store.py` |
| Request-local Memory 检索与有界注入 | `memory/retriever.py`、`context/manager.py` |
| `/memory` 显式管理与 MEMORY.md 投影 | `cli/memory.py`、`memory/index_projector.py` |
| 内置工具 | `tool/builtin/` |
| P4 事件游标 + 候选暂存（崩溃安全 CAS 幂等） | `evidence/cursor.py`、`memory/sqlite_store.py` |
| P4 保守候选抽取 + 敏感/噪声预过滤 | `memory/candidate_extractor.py`、`memory/prefilter.py` |
| P4 LLM MemoryJudge（pydantic 校验 + 修复重试 + 保守失败） | `memory/judge.py` |
| P4 本地去重 + 冲突消解（supersede/版本化） | `memory/dedup.py`、`memory/conflict.py`、`memory/sqlite_store.py` |
| P4 治理编排（Session End / `/memory harvest`） | `memory/governance_service.py`、`session.py` |
| P4 检索重排（§29 来源优先级）+ Progressive Disclosure | `memory/retriever.py`、`tool/builtin/memory_get.py`、`tool/builtin/evidence_get.py` |
| P5 TaskGraph + Planner（LLM/Static，保守失败退化单 Step） | `orchestration/task_graph.py`、`orchestration/planner.py` |
| P5 AgentRuntime（reflection）+ LocalVerifier | `runtime/agent_runtime.py`、`runtime/local_verifier.py` |
| P5 StepScheduler（pending-set 增量派发，非 barrier） | `orchestration/step_scheduler.py` |
| P5 Workspace 隔离（git worktree / 非 git 回退） | `workspace/manager.py`、`workspace/git_worktree.py` |
| P5 MasterRuntime（plan→schedule→verify→replan→merge→cleanup） | `orchestration/master_runtime.py`、`orchestration/master_session.py` |
| P5+ 事务化集成：依赖门控（integrated 才解锁后继） | `orchestration/step_scheduler.py`、`orchestration/integration_coordinator.py` |
| P5+ 乐观并发：base_revision + 读/写集重叠检测 → 过期即最新基线重跑 | `orchestration/integration_coordinator.py`、`workspace/git_worktree.py` |
| P5+ Integrator 兜底（带冲突现场的增强指令重跑） | `orchestration/integrator.py` |
| P5+ Master Attempt Transaction（candidate 隔离 + 产物级验收 + CAS 原子 promote + fail-closed） | `orchestration/master_runtime.py`、`orchestration/global_verifier.py`、`workspace/git_worktree.py` |
| P6 资源锁清理（引用计数驱逐） | `tool/resource_lock.py` |
| P6 Run 持久化/恢复（`/task --resume`；P8 起 Attempt 级幂等恢复） | `orchestration/run_store.py`、`orchestration/master_runtime.py` |
| P6 共享 Memory（Worker 产候选，Supervisor 集中 staging） | `orchestration/shared_memory.py`、`memory/sqlite_store.py` |
| P6 专项 Agent MemoryProfile（读写边界） | `agent/models.py`、`context/manager.py`、`memory/retriever.py` |
| P7 工具副作用双轴（EffectKind × RetryPolicy，独立于并发轴） | `tool/effects.py`、`tool/base.py` |
| P7 CommandPolicy 命令分类（危险拒 / 外部标记 / 本地）+ 可配 allow/deny | `tool/command_policy.py`、`config.py` |
| P7 CommandExecutor 加固（进程树终止 + env 白名单过滤） | `tool/executor.py` |
| P7 推测期禁外部副作用 + ApprovalPolicy（fail-safe 拒）+ DeferredAction 延后上报 | `tool/approval.py`、`tool/deferred.py`、`tool/builtin/run_command.py` |
| P8 Attempt 级幂等崩溃恢复（状态机 + PROMOTING 按真实 HEAD 判定不重推） | `orchestration/run_store.py`、`orchestration/master_runtime.py` |
| P8 孤儿 worktree/branch 回收 + BASE_STALE/replan 预算分离 | `workspace/git_worktree.py`、`orchestration/master_runtime.py`、`context/profile.py` |
| 测试金字塔②③：真实-LLM 场景套件 + Planner 规划质量探针（opt-in） | `scenarios/`（普通 pytest 不跑真实 LLM） |
| P9：19 任务 × 4 维度质量 benchmark | `scenarios/benchmark.py` |
| A1 / A3：Session token 计量与 REPL 交互审批 | `session.py`、`llm/observed_client.py`、`cli/app.py` |
| C7：默认抽取 Worker 记忆候选，仅成功 Attempt 集中暂存 | `orchestration/worker_harvester.py`、`orchestration/master_session.py` |
| A2：审批后的外部动作执行、持久化与恢复 | `orchestration/run_store.py`、`orchestration/master_runtime.py` |
| O1：调用归因、状态时间线、可重建轨迹导出 | `llm/observed_client.py`、`infra/trace.py`、`observability.py` |
| R1：角色模型路由、显式备用模型链与错误分类 | `llm/routing.py`、`llm/client.py`、`config.py` |
| B4/B5 第一期：Podman Worker、文件工具、命令和独立验收接线 | `execution/`、`runtime/worker_sandbox.py`、`tool/sandbox.py` |

## 角色模型与备用链（R1 第一期）

不配置时保持原行为：使用 `CODEAGENT_MODEL`，压缩 Map 仍使用现有 `map_model`，无自动 fallback。
可为以下角色分别设置模型名称（后缀大写）：

`PLANNER`、`WORKER`、`LOCAL_VERIFIER`、`GLOBAL_VERIFIER`、`JUDGE`、`COMPACT_MAP`、`COMPACT_REDUCE`。

例如在 PowerShell 中设置（将占位模型名替换为服务支持的实际名称）：

```powershell
$env:CODEAGENT_MODEL_PLANNER = "planner-model"
$env:CODEAGENT_MODEL_WORKER = "worker-model"
$env:CODEAGENT_MODEL_FALLBACK_PLANNER = "backup-model-a;backup-model-b"
```

- `CODEAGENT_MODEL_<ROLE>` 覆盖该角色的模型名称，保留调用方的温度、输出上限、上下文窗口等参数。
  未配置的角色保留传入的 `ModelConfig`；压缩 Map/Reduce 的单次输出预算仍有效。
- `CODEAGENT_MODEL_FALLBACK` 是所有已识别角色的默认备用链；
  `CODEAGENT_MODEL_FALLBACK_<ROLE>` 覆盖它。角色备用链显式设为空字符串可禁用全局备用链
  （编程配置可使用 `fallbacks={"global_verifier": ()}`）。最多 3 个备用名称，规范化后去重。
- 仅结构化的限流、超时、服务暂时不可用错误触发切换。鉴权、无效参数/模型、上下文超限、未知错误、
  代码异常与取消不触发切换。主模型成功返回后不会因内容质量或工具执行失败重放本次 LLM 调用。
- 切换仅对本次调用有效，不永久更改角色模型。各候选沿用相同消息与工具 schema；必须确保配置模型
  都支持相应功能，并将上下文窗口设为这些模型共同支持的范围。暂不自动探测模型能力或调整上下文。
- 模型名支持 `provider:model`。默认注册 `anthropic`；无前缀时使用默认 Provider。
  CLI 不自动创建其它 SDK 客户端，未知 Provider 在调用前报配置错误。
  编程接入可构造 `RoutingLlmClient({"anthropic": client_a, "other": client_b},
  StaticModelRouter(config.models))`，再传给 `AgentSession` / `MasterSession`；客户端实现同一 `LlmClient` 协议。
- 自定义 Provider 应抛出带 `LlmErrorKind` 的 `LlmError`；默认 `UNKNOWN` 保守地不切换。
  Anthropic 适配器按 HTTP 状态及异常类型分类，不从异常文本猜测。SDK 内部重试仍由 SDK 控制。

每次候选调用分别写入 O1 的 `llm_call`，带 `route_id`、`route_attempt`、`provider` 与错误分类；
切换记录为 `model_fallback`，累加 `llm.fallbacks`，轨迹的 `by_model` 使用 `provider:model` 分组。
成功调用只计一次用量，未知用量不当成零费用。显式定价与Worker成本阈值路由已补齐，见末节；通用任务特征与硬预算待做。

## 任务轨迹与用量（O1）

默认 `build_master` 装配会启用 RunStore，每次任务结束或异常退出时尝试导出
`state_root/metrics/<mrun_id>.json`。REPL 显示路径；使用 `/trajectory <mrun_id>` 可从 RunStore
与事件日志重新生成并查看摘要。自动导出默认最多等待 10 秒；失败或超时计入
`observability.export_failures`，不改变任务结果。

报告包含 Attempt 状态转移及相邻状态间耗时、分 Attempt 的 Step 结果、Worker 与工具事件引用、
工具执行耗时、延后外部动作状态，以及按模型、角色、Attempt、Step、Worker、启动/恢复次数
（`invocation_id`）分组的 LLM 用量。旧 Step 结果保留在兼容字段中，不伪造历史。

- `totals` 统计该任务跨 Session、跨恢复的已记录调用；`session_cumulative_snapshot` 是自动导出时
  **当前会话累计**指标，两者不能直接比较。单个任务的 token 应与该任务前后 `/metrics` 的增量比较。
- 默认装配覆盖 Planner、Worker、Local/Global Verifier、Memory Judge、Map/Reduce 压缩。
  会话结束的记忆治理属于 Session，不强行归到某个已结束任务；初始规划没有 Attempt/Worker ID。
  自定义注入的组件应使用 `session.llm_client`，并可用 `RoleLlmClient` 指定角色。
- `llm.calls` 表示成功返回的逻辑调用，`llm.attempts` 包含失败和取消；SDK 内部重试不单独拆分。
  失败/取消时未知的用量保留为 `null`，不假定为免费。输入、输出、缓存读写 token 分列记录。
- 未配置价格时金额仍为null/pricing_not_configured；启用价格后区分known/partial及RunStore同步成本，不把未知当零。
- 调用记录不复制 prompt、响应正文或异常消息。JSON 轨迹是可重建投影，恢复仍只读取 RunStore。
  异步日志在崩溃时可能丢失未落盘事件，旧运行也可能缺少关联字段；报告明确标注覆盖边界。
- 当前导出扫描本项目所有 Session 日志以关联跨会话恢复；适用于现有本地规模，尚未加入日志索引或 OTel。

## 未实现（按分期）

- **验证缺口**：**P9 起**已有小规模质量 benchmark（`scenarios/benchmark.py`，19 任务×4 维度分层，
  产成功率/首次成功率/假接受/成本聚合）；SWE-bench 待接（仅留接入笔记），假拒绝精确归因待补。
  （P8 起 Attempt 级幂等崩溃恢复：
  PROMOTING 窗口按真实 base HEAD 判定不重复推进、孤儿 worktree/branch 回收、BASE_STALE 独立预算。）
- **外部副作用隔离**：Master Attempt 的 candidate 只隔离仓库内文件；Worker 的外部副作用
  （API/DB/树外写/发布/`run_command` 写绝对路径）不被事务覆盖。已识别的外部命令会延后执行，
  但命令分类不能替代容器沙箱和网络隔离。
- **跨进程恢复锁**：同一 run 仅支持单进程恢复；不要同时启动两个 resume。

P2/P3/P4/P5 都采用保守失败：压缩失败不替换 History，Memory 检索失败不阻断对话，
Judge/治理链失败宁可不写长期 Memory，Planner/Verifier 失败退化/放行不卡编排，
最终越过 hard limit 才抛 `ContextOverflowError`，绝不静默截断历史。

## 安全说明

`run_command` 执行模型给出的 shell 命令。**P7 起**已加：`CommandPolicy` 命令分类（危险命令拒绝、
外部副作用识别，可配 `CODEAGENT_CMD_ALLOW/DENY`）、`CommandExecutor` 加固（**进程树终止**、
**env 白名单过滤**不泄露密钥）、**推测执行期禁止不可回滚的外部副作用**（网络/发布/DB/部署→被拦并记为
待处理动作）、放行阶段经 `ApprovalPolicy`（默认 fail-safe 拒绝）。

默认 `local` 后端仍在本机执行。`SandboxExecutor` 已实现，显式选择 `podman` 后使用下述隔离流程；
当前在独立 Ubuntu VM 完成真实容器验收；Windows 与 VM 证据分别记录，WSL 仅保留历史结果。

### Podman 沙箱（B4/B5 第一期）

目标环境为 Linux、rootless Podman、cgroup v2，并具有 CPU/memory/pids 控制器。
镜像需由操作者预先安装并信任，包含 `python`、`/bin/sh` 及任务需要的依赖；必须配置完整 SHA256
镜像 ID。后端不自动拉取镜像，也不自动退回本机执行。

```bash
export CODEAGENT_EXECUTION_BACKEND=podman
export CODEAGENT_SANDBOX_IMAGE='<已安装的完整 SHA256 镜像 ID>'
export CODEAGENT_VERIFY_CMD='python -m pytest -q'
python -m codeagent.cli.app --workspace /path/to/git-repo
# 在 REPL 中直接输入目标，或使用 /task <目标>
```

每次 Worker（包括 reflection）拥有一个独立容器。`read_file` / `write_file` / `grep` 和
`run_command` 都访问容器 `/workspace`；新自定义工具必须先适配沙箱，否则拒绝运行。
Memory、Evidence 与 Artifact 的固定内置读取工具由控制面提供。

输入来自本次私有目录（Git worktree 或非 Git 快照候选）的有界数据快照，排除 `.git`、`.codeagent`、`.env` 与 `.env.*`。
容器没有宿主 worktree 挂载、没有网络、根文件系统只读；项目与临时数据使用限额 tmpfs。
启动时在导入项目文件前检查实际 UID、capabilities、seccomp、网络接口和 cgroup 限额。
默认每域 1 CPU / 512 MiB 内存 / 64 进程，workspace 256 MiB、临时目录 64 MiB、单命令
60 秒、输出 4 MiB；Python 调用方可用 `AppConfig.sandbox_limits` 调整。

Worker 与本地验证器均成功后，先冻结容器，由可信宿主辅助进程读取快照，再销毁容器。
输出通过完整路径、文件类型和容量校验后进入私有 staging，再写入该 Worker 的隔离 worktree；
后续沿用 candidate 集成和 CAS promote。链接、特殊文件、保护路径以及 `.gitattributes` /
`.gitmodules` 改动会被拒绝；Git 控制面使用操作者信任的仓库配置。快照上限默认 4096 个文件与目录、
单文件 8 MiB、总内容 64 MiB。失败、取消或清理失败的 Worker 不发布输出。

确定性验收使用另一个容器，其文件改动不回传。退出码失败或沙箱异常会阻止 promote。
Git 与非 Git 的 `/task` 和普通单 Agent 交互均已接入；默认 local 后端仍在本机执行。
干净 Git 根工作区的普通交互每轮创建隔离候选，
文件工具和命令在同一个容器执行；上下文跨轮保留，容器、临时文件及迭代预算不跨轮复用。
读任务不创建项目提交；有文件改动时必须配置 `CODEAGENT_VERIFY_CMD`，在独立验收容器通过后
自动创建候选提交并 CAS fast-forward 到项目，成功结果仅列出实际发布文件。缺验收、验收失败、
取消、封存或删除失败均不发布；用户并发修改或 HEAD 变化会拒绝合并。`/clear` 只重置对话上下文。
普通交互不在宿主执行被延后的外部动作；ReAct 完成与发布结果分别记录，最终状态见
`sandbox_turn_finished`，不能仅把模型声称完成当成成果已发布。
沙箱模式的 post-promote 外部动作记为跳过（中断动作保留 unknown），即使已审批也不会交给宿主执行。
容器中没有 Git 元数据；联网安装依赖及发布操作不在本期支持范围内。

2026-10-01 一期接线的历史 Windows 回归：**356 passed, 39 skipped**；Ruff、Pyright 通过。
跳过项为 25 个 POSIX 快照测试、8 个 POSIX 发布测试、1 个既有 POSIX 执行器测试和 5 个真 Podman 测试。
同日已补齐 Ubuntu WSL2 的 Podman、Python 测试依赖和固定摘要官方镜像。新增拒绝回归后，
Linux 全量 **395 passed、1 skipped**，Ruff/Pyright 通过，6 个真容器用例全部通过；
断网、只读根目录、资源限额、快照回传与清理实测通过，容器清单为空。
唯一跳过是 Linux 上不适用的“不支持平台应拒绝”测试；证据、镜像身份与复现见
[`LINUX_SANDBOX_ACCEPTANCE.md`](LINUX_SANDBOX_ACCEPTANCE.md)。
Linux 上的验证入口如下（不会自动安装运行时或镜像）：

```bash
export MINDCODE_PODMAN_TEST_IMAGE='<已安装的完整 SHA256 镜像 ID>'
python -m pytest -q tests/test_execution_snapshot.py tests/test_sandbox_workspace.py tests/test_podman_integration.py
```

跨进程回收已接入 `/task`：创建前记录意图，SQLite 私有账本配合内核 flock 租约，启动时只回收
同项目账本内失去控制器租约且身份完全匹配的资源。SIGKILL 的未绑定 ID / 暂停窗口实测通过，
活跃实例保留；删除失败或存在性未知保留记录并拒绝继续。不扫描其他容器。
独立 Ubuntu VM（`6.8.0-142-generic`）追加全量 **415 passed、1 skipped**，Ruff / Pyright 通过，
8 个真容器用例和独立内核探针通过，容器清单为空；与此前 WSL2 395 / 1 分开记录。
待完成：更广的按需网络策略；非 Git持久化恢复见末节。受控文件下载见下节；资源账本与 C6 run 恢复锁职责不同，后者见下节。

### 受控 HTTPS 文件下载（B5 第一期）

Podman 模式可显式配置 `CODEAGENT_DOWNLOAD_HOSTS`（分号分隔的精确小写 DNS 主机名），
配置后才注册 `download_file(url, sha256, path)`。空白名单默认不开放下载。

```bash
export CODEAGENT_DOWNLOAD_HOSTS='files.pythonhosted.org'
# CODEAGENT_DOWNLOAD_APPROVAL 默认为 prompt：REPL 逐次询问，非交互默认拒绝。
# 受信自动化可显式设为 allowlist，这是操作者的持续下载授权。
```

只允许 HTTPS:443 GET，无凭据、查询或片段。URL、精确域名、公网 DNS 在每个重定向跳重新检查；
连接固定到已检查 IP，TLS 证书/SNI 仍按域名验证。可信下载子进程不继承 API 密钥或代理环境，
默认最多 8 MiB、30 秒、3 次跳转；Python 调用方可通过 `DownloadPolicy` 在固定上限内调整。
模型必须提供可信来源的 SHA256，下载成功不等于内容安全，摘要本身也不是供应链认证。

授权请求前落盘审计意图；审计等待最多 5 秒，失败或卡住时不发请求。结束记录跳转/IP/状态/字节，
不记录响应正文。超时或取消停止并等待子进程；已经发出的 GET 无法被候选回滚撤销。
校验后的二进制只导入当前离线容器，路径锚定防链接逃逸，不覆盖不同内容的已有文件。
`/task` 与普通交互都继续经过独立验收再发布；`downloaded` 审计状态只表示字节已校验。
容器 shell 直接联网仍被阻断；工具不提供在线 pip/npm 安装、POST、发布或通用 TCP。

本期独立 Ubuntu VM **562 passed、1 skipped**，27 个真容器用例通过；Windows **435 passed、128 skipped**，两端 Ruff / Pyright 通过；新增 9 个真容器下载用例覆盖二进制导入/复用/冲突/父目录链接、
真实下载且直接 TCP 失败、任务与普通交互的接受/拒绝。Linux 仅在独立 VM
`/home/mengx/mindcode-download-final.HSweo1` 验证，无容器残留，原仓库与历史失败现场保留。
策略和复现见 [`NETWORK_DOWNLOAD_DESIGN.md`](NETWORK_DOWNLOAD_DESIGN.md) 及
[`LINUX_SANDBOX_ACCEPTANCE.md`](LINUX_SANDBOX_ACCEPTANCE.md)。B5 整项仍未完成。

### 同 run 跨进程恢复互斥（C6）

默认 SQLite 装配自动为新任务与 `/task --resume` 取得同 run 内核租约；先持锁再读取恢复状态，
锁覆盖编排、孤儿清理、验收/合并、外部动作、轨迹导出。另一进程立即返回忙碌，不能读旧状态后重复执行。
同数据库路径使用同一锁目录；不同 run 不相互排斥。Linux 用 flock，Windows 用非阻塞字节锁。
进程退出释放锁，锁文件保留；锁不可用拒绝运行，不能删文件或用 TTL 强行接管。

RunStore 取消及重复取消时先等待 SQLite 线程结束，再释放 run 租约；恢复只回收当前 run 已记录的
候选/Worker 分支，不对项目全局 prune，未知资源保留。默认 NullRunStore 只有同 Runtime 的进程内互斥，
自定义持久化存储需要显式共享锁命名空间。保证限于同主机、同规范 SQLite 路径与受保护的本地控制目录。
C6负责执行权互斥，不保证外部exactly-once；非Git恢复与staging回收已由末节机制补齐。

独立 Ubuntu VM **580 passed、1 skipped**，27 个真容器用例通过；Windows **449 passed、132 skipped**，两端 Ruff/Pyright 通过；C6 专项18项计入 VM 全量，Windows其中4项POSIX路径保护跳过。
真实双进程恢复/强杀接管、取消线程排空、导出持锁及其他 run 资源保留通过。下载/容器27项继续回归通过，
本期没有重跑历史真实模型套件。设计与边界见 [`RUN_RECOVERY_LOCKING.md`](RUN_RECOVERY_LOCKING.md)。

### 延后外部动作的恢复边界（A2）

动作清单在 promote 前写入 `RunStore`，按 run / Attempt / 动作 ID 隔离；执行前记录 `running`
和累计尝试次数，执行后记录结果。清单或执行前记账失败时停止，不继续产生副作用。
REPL 会逐条询问审批；非交互装配默认拒绝。命令在真实仓库根目录执行。

| 持久化状态 | `/task --resume <mrun_id>` 行为 |
|---|---|
| `pending` | 重新审批后执行 |
| `succeeded` | 保留成功结果，不再执行 |
| `running`（进程在执行中或结果落库前中断） | 仅 `IDEMPOTENT` 且剩余预算足够时，重新审批后重试；否则记为 `unknown` |
| `failed` / `skipped` / `unknown` | 保留结果，不自动重试；失败或未知需人工核对 |

`NEVER` 最多尝试一次，`IDEMPOTENT` 累计最多两次（包括恢复前的尝试）；其他策略保守地最多一次。
动作 ID 仅用于本地去重，不自动构成外部服务的幂等键，也不保证外部系统 exactly-once。
外部动作失败不回滚已合并代码，CLI 会分别显示成功、失败、跳过和结果未知。
拒绝审批或缺少执行环境会成为终态 `skipped`，恢复不会再次自动申请执行。

旧数据库启动时自动新增动作表；升级前没有持久化的动作无法追溯恢复。
编程装配使用 `NullRunStore` 时没有跨进程恢复能力。恢复保证针对单进程中断，不包含跨进程并发执行。

2026-10-02 普通交互追加验收：独立 Ubuntu VM **428 passed、1 skipped**，10 个真容器用例通过；
Ruff / Pyright 通过，真实 REPL 的写入、连续读取、/clear、退出冒烟通过，容器清单为空。
Windows **356 passed、73 skipped**；真实模型历史场景本轮未重跑。


2026-10-02 共享工作区追加验收：独立 Ubuntu VM **454 passed、1 skipped**，14 个真容器用例通过；
Windows **356 passed、99 skipped**，Ruff / Pyright 通过。含未提交修改的 Git 与非 Git 普通交互均通过
真实 REPL 写入、连续读取、/clear、退出冒烟，最终无容器残留；全程未在 WSL 跑测试。

含暂存、未暂存或未跟踪文件的 Git 工作区采用当前工作文件快照，验收后只回写差异，保留暂存区与 HEAD，
不自动提交。不读取忽略的非跟踪文件；已有被跟踪文件即使匹配 ignore 规则仍纳入输入。非 Git 普通交互
使用同一发布机制，不创建仓库。控制面状态、env、常见非 Git 虚拟环境与缓存不进入模型执行域。
输出忽略产物在独立验收前过滤，验收不能依赖随后会丢弃的文件。

共享写回先持久化 prepared 日志，再逐文件原子替换并 fsync，最后写入 applied 决定；恢复会回滚未完成
的差异，已 applied 的成果保留。发现用户的新修改、损坏记录或发布决定确认失败时报告“待核对”并保留记录。
日志在 state_root/publication/<工作区路径摘要>，目录0700、文件0600，保存有界前后文件内容，模型不可访问。
MindCode 写入使用 flock 串行化，并在输入与写回前后复查工作树、HEAD、分支和暂存内容。该锁无法约束
不遵守租约的外部编辑器，多个文件的可见性也不是全局原子事务；文件与目录互相转换暂时拒绝。


2026-10-02 非 Git `/task` 追加验收：独立 Ubuntu VM **475 passed、1 skipped**，18 个真容器用例通过；
Windows **357 passed、119 skipped**，Ruff / Pyright 通过。真实 REPL 的 `/task` → 普通读取 → /clear → 退出
通过，容器清单为空。先提交共享交互阶段 `890bae7`，本节新实现尚未提交；未使用 WSL 测试。

非 Git `/task` 使用数据快照候选，Worker 各自隔离；成功冻结/销毁容器后只向私有候选集成差异。
前驱集成后才解锁后继；并行兄弟发现读写重叠，或 shell/搜索读集未知且候选已变时，丢弃旧输出再重跑。
整批冻结成内容摘要，提供真实文件差异与独立容器确定性验收给全局门禁。包含写入时必须配置
CODEAGENT_VERIFY_CMD；缺验收、失败或结果不确定均不接受，拒绝的 Attempt 从原始快照重建，避免重复追加。
通过后复用共享写回日志与项目级租约，只发布实际差异；不创建 .git 或 Git 提交，不在宿主执行延后外部动作。
已集成 Worker 的私有目录与无引用修订及时释放；最终文件清单只列实际发布文件。

非 Git 任务取消先停止并等待所有 Worker 的容器清理，再删除私有 staging，RunStore 记录 cancelled。
发布冲突或决定未知沿用“待核对”处理。当前 snapshot:<SHA256> 是内容修订标识，不是 Git SHA，
candidate/worker 标识是内部引用。以下是最初阶段的边界，本期持久化恢复已补齐，见末节。
最初非 Git 沙箱 `/task --resume` 明确拒绝，不修改旧记录，CLI 也不提示可以恢复；请核对实际成果后新建任务。
控制器 SIGKILL 后容器仍按资源账本精确回收；私有磁盘 staging 尚无跨进程自动清理，任务恢复与租约不能混淆。
共享写回仍是逐文件事务：外部编辑器不遵守租约时存在检查/写入竞态，不提供全文件系统原子 CAS。


2026-10-02 提交前复验：独立VM以固定含pytest镜像运行真实deepseek-flash，历史场景 **11/11通过**，
非Git任务正/负例 **2/2通过**，并验证任务后普通交互；verify_fail拒绝两次Attempt后base不推进且无note.txt。
之后VM全量重新通过475 / 1（18真容器），Windows重新通过357 / 119，静态检查通过，无容器残留。
原仓库与历史失败现场保留，未使用WSL；证据和单次评测边界见Linux沙箱验收记录末节。


### 非Git任务持久化恢复与staging回收（2026-10-02）

独立 Ubuntu VM **611 passed、1 skipped**，30 个真容器用例通过；Windows **459 passed、153 skipped**，两端 Ruff/Pyright 通过。新增31项计入全量，含3项真实Podman控制器SIGKILL窗口；恢复专项62项通过，
不与全量相加。模型用Stub固定步骤，历史真实LLM11/11与非Git2/2仍是此前独立证据。

默认SQLite装配支持非Git /task --resume：保存原始/最新冻结快照、目录身份/策略绑定和发布回执。
未到PROMOTING从保存DAG和原始输入重跑；PROMOTING复用已验收候选，只重试发布；
applied文件日志先保留，SQL FULL同步记账后退休，恢复不重复写回，发布后人工修改保留。
无回执而目录变化时即便恰好等于候选也拒绝，损坏记录和身份不符均保留供核对。
新staging账本先持owner租约并记意图，再mkdir/绑定inode/写数据；只回收已记账且owner已退出的目录。
候选、Worker、validation及交接临时目录均在owner根下；活动/未知目录保留，替换/链接/删除失败不猜删。
旧run无快照、NullRunStore或无适配器定制存储仍拒绝resume；跨run归档、外部编辑器全局CAS及分布式恢复待设计。
设计与边界见 [NON_GIT_RECOVERY_DESIGN.md](NON_GIT_RECOVERY_DESIGN.md)。

以上阶段已提交92c88d1；R1成本阈值路由见末节。B5通用联网及Phase2/3仍待推进。
带日期的旧段落保留当时证据，当前状态以本节和开头进度为准。


### R1二期：显式定价与Worker成本阈值路由（2026-10-02）

独立 Ubuntu VM **647 passed、1 skipped**，30 个真容器用例通过；Windows **495 passed、153 skipped**，两端 Ruff/Pyright 通过。新增36项计入全量，金额/路由/轨迹专项66项通过，不另加总。
上一阶段B5/C6及非Git恢复已提交92c88d1；本期新改动尚未提交。

CODEAGENT_MODEL_PRICES指定USD/百万token价格JSON；CODEAGENT_WORKER_COST_THRESHOLD_USD与
CODEAGENT_MODEL_ECONOMY_WORKER成对启用Worker阈值降级，默认不配置时保持原行为。
输入/输出/缓存读写分别按显式单价计价，内部整数picodollar；价格文件不内置供应商现价。
请求前向原runs.db同步记成本意图，返回后记金额或unknown；恢复沿用本run成本，旧历史无覆盖也不当零。
轨迹同时显示RunStore同步成本下界与事件投影。模型、角色、价格及切换原因有观测记录，异步事件失败不改路由决定。

已知成本达阈值或有未知项时，后续Worker尝试指定经济模型；Planner/Verifier等角色保留原配置。
经济模型必须声明工具/窗口/输出能力，不适用时保留原模型；现有TokenEstimator估算，不新增热路径精确计数。
这是软阈值：并发在途、验收器、能力拒绝和显式fallback都可能使实际费用超阈值，不承诺账单硬上限。
普通交互无Master scope，不按本期任务阈值降级。1小时缓存写等未识别计价类别保守unknown。
配置与边界见 [R1_COST_ROUTING.md](R1_COST_ROUTING.md)。

金额和角色测试使用固定Provider，无新付费模型调用，不宣称已证明真实成本节省或质量不降。
下一项仍需完整模型能力/通用任务特征适配，以及真实模型质量与成本评测；硬预算另行设计。
