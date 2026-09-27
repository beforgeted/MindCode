# MindCode CodeAgent

Python 实现的编码 Agent。设计文档见仓库根目录的四份 md，落地方案与分期见
[`MindCode_实现设计_V1.md`](MindCode_实现设计_V1.md)。

当前落地范围：**P0–P6**（单 Agent + Multi-Agent 执行骨架 + 编排持久化/恢复与共享 Memory）。

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
`/task --resume <mrun_id>` 恢复：未 promote 的 run 从干净 Attempt 整体重开（副作用不可信重放）。
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
| P6 Run 持久化/恢复（事务式，未 promote 即整体重开，`/task --resume`） | `orchestration/run_store.py`、`orchestration/master_runtime.py` |
| P6 共享 Memory（Worker 产候选，Supervisor 集中 staging） | `orchestration/shared_memory.py`、`memory/sqlite_store.py` |
| P6 专项 Agent MemoryProfile（读写边界） | `agent/models.py`、`context/manager.py`、`memory/retriever.py` |

## 未实现（按分期）

- **验证缺口**：真实模型 Planner/Verifier 大规模 benchmark；跨进程 verify/promote 中途崩溃的
  精细断点恢复（当前为"未 promote 即整体重开"）；Worker 候选自动抽取器
  （`AgentRuntime.candidate_harvester`）的默认接线。
- **外部副作用隔离**：Master Attempt 的 candidate 只隔离仓库内文件；Worker 的外部副作用
  （API/DB/树外写/发布/`run_command` 写绝对路径）不被事务覆盖，需后续用幂等键 / 两阶段提交 /
  延后执行 / 不可重试标注治理（属 `run_command` sandbox 缺口）。

P2/P3/P4/P5 都采用保守失败：压缩失败不替换 History，Memory 检索失败不阻断对话，
Judge/治理链失败宁可不写长期 Memory，Planner/Verifier 失败退化/放行不卡编排，
最终越过 hard limit 才抛 `ContextOverflowError`，绝不静默截断历史。

## 安全说明

`run_command` 执行模型给出的任意 shell 命令，只挡了几条明显破坏性的命令，
**没有**用户确认或沙箱机制。在不受信任的环境使用前必须先补 gating。
