# MindCode CodeAgent

Python 实现的编码 Agent。设计文档见仓库根目录的四份 md，落地方案与分期见
[`MindCode_实现设计_V1.md`](MindCode_实现设计_V1.md)。

当前落地范围：**P0–P9**（单 Agent + Multi-Agent 事务化编排 + 编排持久化/恢复与共享 Memory
+ 工具执行安全 + Attempt 级幂等崩溃恢复 + 小规模质量 benchmark）。
收尾接线 **A1 / A3 / C7 / A2** 已实现：会话计量、交互审批、Worker 记忆候选抽取与延后外部动作恢复。
优化 **O1** 已实现：角色/模型调用归因、Attempt 状态历史与任务轨迹 JSON 导出。

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
- 金额成本为 `null` / `pricing_not_configured`：尚未配置模型价格和缓存计费规则，不显示虚假的零费用。
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

**仍缺**（后续 Phase）：完整容器**沙箱**（`SandboxExecutor` 仅接口占位）与网络策略真隔离。
在完全不受信任的环境仍需补沙箱。

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
