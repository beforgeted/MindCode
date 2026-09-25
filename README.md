# MindCode CodeAgent

Python 实现的编码 Agent。设计文档见仓库根目录的四份 md，落地方案与分期见
[`MindCode_实现设计_V1.md`](MindCode_实现设计_V1.md)。

当前落地范围：**P0–P5**（单 Agent + Multi-Agent 执行骨架）。P6 恢复能力仍未实现。

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
`/task <目标>` `/clear` `/metrics` `/quit`。`/task` 走 P5 Multi-Agent：规划 → 并行 Worker
（git 仓库下各自 worktree 隔离，非 git 回退只读并行+写串行）→ 验收 → 合并回 base。

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

## 未实现（按分期）

- **P5 验证缺口**：真实模型 Planner/Verifier benchmark、并行写隔离的大规模压测
- **P6** 资源锁清理 / Run 持久化与恢复 / Multi-Agent 共享 Memory（Worker 产候选、Supervisor 集中写）/ 专项 Agent MemoryProfile

P2/P3/P4/P5 都采用保守失败：压缩失败不替换 History，Memory 检索失败不阻断对话，
Judge/治理链失败宁可不写长期 Memory，Planner/Verifier 失败退化/放行不卡编排，
最终越过 hard limit 才抛 `ContextOverflowError`，绝不静默截断历史。

## 安全说明

`run_command` 执行模型给出的任意 shell 命令，只挡了几条明显破坏性的命令，
**没有**用户确认或沙箱机制。在不受信任的环境使用前必须先补 gating。
