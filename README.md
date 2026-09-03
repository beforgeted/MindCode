# MindCode CodeAgent

Python 实现的编码 Agent。设计文档见仓库根目录的四份 md，落地方案与分期见
[`MindCode_实现设计_V1.md`](MindCode_实现设计_V1.md)。

当前落地范围：**P0（地基）+ P1（Evidence 平面 + Tool 结果治理 + ContextManager 外壳）**。

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

REPL 命令：`/context` `/compact` `/memory` `/clear` `/metrics` `/quit`。

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
| 内置工具 | `tool/builtin/` |

## 未实现（按分期）

- **P2** HistoryCompactor / TaskCheckpoint / Turn 状态机 — 挂载点在 `context/compact/base.py`
- **P3** SQLite Durable Memory / `/memory`
- **P4** Memory 写入治理 + 混合检索
- **P5** Multi-Agent 并行 + Workspace 隔离
- **P6** 资源锁清理 / Run 持久化 / 共享 Memory

P1 没有 Compactor，所以上下文越过 hard limit 时会抛 `ContextOverflowError`
而不是静默截断历史 —— 这是刻意的，见 `context/manager.py` 的注释。

## 安全说明

`run_command` 执行模型给出的任意 shell 命令，只挡了几条明显破坏性的命令，
**没有**用户确认或沙箱机制。在不受信任的环境使用前必须先补 gating。
