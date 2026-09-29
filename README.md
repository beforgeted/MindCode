# MindCode CodeAgent

Python 实现的编码 Agent。设计文档见仓库根目录的四份 md，落地方案与分期见
[`MindCode_实现设计_V1.md`](MindCode_实现设计_V1.md)。

当前落地范围：**P0–P8**（单 Agent + Multi-Agent 事务化编排 + 编排持久化/恢复与共享 Memory
+ 工具执行安全 + Attempt 级幂等崩溃恢复）。

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
`/task <目标>` `/clear` `/metrics` `/quit`。`/task` 走事务化 Multi-Agent：规划 → 并行 Worker
（各自 git worktree 隔离，集成进 candidate 而非真实 base）→ 依赖门控/过期重跑/Integrator 兜底
自愈冲突 → **产物级全局验收** → 通过才 **CAS 原子推进真实 base**，否则整个 Attempt 丢弃、从
起点重开（用户只看到"任务完成/未完成"，不接触 git 冲突）。非 git 环境回退只读并行+写串行。
`/task --resume <mrun_id>` 恢复：Attempt 级幂等恢复——已 promote 的识别为完成不重推，
PROMOTING 崩溃窗口按真实 base HEAD 判定，其余状态回收孤儿后从持久 original_base 重开（P8）。
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

## 未实现（按分期）

- **验证缺口**：**P9 起**已有小规模质量 benchmark（`scenarios/benchmark.py`，19 任务×4 维度分层，
  产成功率/首次成功率/假接受/成本聚合）；SWE-bench 全集待接（仅留接入笔记）。Worker 候选自动抽取器
  （`AgentRuntime.candidate_harvester`）的默认接线待做。（P8 起 Attempt 级幂等崩溃恢复：
  PROMOTING 窗口按真实 base HEAD 判定不重复推进、孤儿 worktree/branch 回收、BASE_STALE 独立预算。）
- **外部副作用隔离**：Master Attempt 的 candidate 只隔离仓库内文件；Worker 的外部副作用
  （API/DB/树外写/发布/`run_command` 写绝对路径）不被事务覆盖，需后续用幂等键 / 两阶段提交 /
  延后执行 / 不可重试标注治理（属 `run_command` sandbox 缺口）。

P2/P3/P4/P5 都采用保守失败：压缩失败不替换 History，Memory 检索失败不阻断对话，
Judge/治理链失败宁可不写长期 Memory，Planner/Verifier 失败退化/放行不卡编排，
最终越过 hard limit 才抛 `ContextOverflowError`，绝不静默截断历史。

## 安全说明

`run_command` 执行模型给出的 shell 命令。**P7 起**已加：`CommandPolicy` 命令分类（危险命令拒绝、
外部副作用识别，可配 `CODEAGENT_CMD_ALLOW/DENY`）、`CommandExecutor` 加固（**进程树终止**、
**env 白名单过滤**不泄露密钥）、**推测执行期禁止不可回滚的外部副作用**（网络/发布/DB/部署→被拦并记为
待处理动作）、放行阶段经 `ApprovalPolicy`（默认 fail-safe 拒绝）。

**仍缺**（后续 Phase）：完整容器**沙箱**（`SandboxExecutor` 仅接口占位）、延后外部动作的真正
post-promote 执行、网络策略真隔离。在完全不受信任的环境仍需补沙箱。

