# MindCode 实现设计 V1

> 本文档不重复四份设计文档的内容，只回答三个问题：
> **跨文档冲突怎么裁决、分几期做、代码里强制了哪些不变式。**
>
> 上游文档：
> - `CodeAgent 上下文管理与压缩机制设计文档.md`（2026-08-20，下称**上下文文档**）
> - `CodeAgent记忆系统V2设计文档.md`（2026-08-26，下称**记忆 V2**）
> - `CodeAgent_Multi-Agent_并行架构设计方案_Python.md` / `_Java.md`（下称**并行文档**）

---

## 1. 语言与版本主线

**Python 3.11+，asyncio。** 3.11 是硬底线且是**载荷性**的：`asyncio.TaskGroup`、
`asyncio.timeout`、`StrEnum` 三个特性都在关键路径上。当前开发环境 3.12。

Java 版并行文档降级为参考资料，不再同步维护 —— 同一套设计维护两份语言实现
必然漂移。

数据结构约定：

| 用途 | 选择 | 理由 |
|---|---|---|
| 内部值对象 | `@dataclass(frozen=True, slots=True)` | 便宜、可哈希、无魔法 |
| 跨 LLM 边界的结构 | pydantic v2 | 需要 JSON schema 生成 + 模型产出校验 + 修复重试 |
| 状态枚举 | `StrEnum` | 并行文档里 `status: str = "created"` 和 `Enum` 两种写法并存，统一掉 |

pydantic 的落点是 P2 起的 `TaskDelta` / `TaskCheckpoint` 和 P4 的
`MemoryCandidate` / MemoryJudge 输出。P0/P1 尚未涉及 LLM 结构化输出，
所以现在全部是 dataclass。

---

## 2. 五处跨文档裁决

### 2.1 谁是事实源 —— 以记忆 V2 为准

上下文文档 §2.1 定 `conversationHistory` 为"唯一事实源"；记忆 V2 §5.2 改成：

```
RawEventStore        = Durable Historical Truth
ConversationHistory  = Active LLM Working History
```

V2 更晚且更对：History 会被 prune / offload / compact / 换成 Checkpoint，
一个可被有损改写的东西不能叫事实源。

**后果**：`RawEventStore` 必须进 P1，不能像上下文文档那样排在计划外 ——
`EvidenceRef` 和 tool 结果 offload 都指向它。

### 2.2 先裁剪还是先摘要 —— 裁剪先做

上下文文档把 Compactor 重写排 Phase 1、ToolResultPruner 排 Phase 2。**倒过来。**

三层理由，第三层是硬依赖：

1. 上下文文档自己的原则 §40.2 就是"先裁剪，再摘要"；
2. 裁剪是确定性的、零 LLM 调用、无有损风险，先吃掉这部分收益能显著降低
   Compaction 的触发频率，也就缩小了 Compactor 这个高风险组件的爆炸半径；
3. §29 承认单条 300K 的 tool 输出 Compactor 解决不了，而 §17 的 chunking
   要求"保持 Turn 原子性" —— 若一个 Turn 里躺着 300K 的 tool result，
   就永远切不出 ≤20K 的 chunk。**tool 边界的有界化是 token-aware chunking
   能够成立的前提，不是优化项。**

### 2.3 ConversationTurn 的排期 —— 提前到 P2

上下文文档把 Turn 模型排到最后一期（Phase 5），但它 §35 的 Compactor 伪代码
第一行就是 `turnPartitioner.partition(history)`。Compactor、并行 ToolCall、
Cancellation、SubAgent 四件事全部以 Turn 为协议边界。

当前实现：`context/history/turn.py` 里已有 `ConversationTurn` / `TurnStatus` /
`TurnIdPartitioner`，`Message` 带 `turn_id`。接口已钉住，P2 换成带状态机的
实现时调用方不用改。

### 2.4 AgentResult 的形状 —— 以记忆 V2 §32 为准

并行文档 §9 的 `AgentResult.success(run_id, content)` 是个字符串，
满足不了记忆 V2 §47.7（Worker 产 80K tool history，Master 只收结构化结果）。
采用结构化的 `AgentRunResult`：decisions / files / tests / open_issues /
evidence_refs / memory_candidates。Worker 的过程细节通过 `EvidenceRef` 下钻，
不进 Master 的 Context。

见 `agent/models.py`。

### 2.5 并行文档里没有 ContextManager —— 最关键的一条缝

并行文档 §5 的 `RunContext` 直接持 `list[Message]`，§9 的 `ReActEngine` 直接
`llm_client.chat(run.context.messages, ...)`，中间没有任何上下文准备。这和
上下文/记忆文档要求的"每次 LLM 调用前必过 `ContextManager.prepare()`"正面冲突。

**如果等到 P5 才发现这件事，`ReActEngine` 要整个重写。** 所以从第一行代码起：

- `AgentRun.history` 是 `ConversationHistory`，不是 `list[Message]`；
- `ConversationHistory` 是 **run-scoped** 的，构造时就要 `session_id` +
  `agent_run_id`，不是全局单例；
- 每个 `AgentRun` 自带 `ContextProfile`（P2 起还带自己的 `TaskCheckpoint`）；
- `ReActEngine` 只调 `context_manager.prepare()`，完全不知道裁剪/压缩策略。

见 `agent/run.py`、`runtime/react_engine.py`。

---

## 3. 分期路线

依赖是严格线性的：**P0 → P1 → P2 是脊柱**，之后 {P3→P4} 和 {P5→P6} 是两条
几乎不相交的轨道，只在"Multi-Agent 共享 Memory"处重新汇合。

| 期 | 内容 | 状态 |
|---|---|---|
| P0 | 地基与可观测 | ✅ 已完成 |
| P1 | Evidence 平面 + Tool 结果治理 + ContextManager 外壳 | ✅ 已完成 |
| P2 | Turn 状态机 + Compaction 重写 + TaskCheckpoint | ⬜ 挂载点已备好 |
| P3 | 本地 Durable Memory 最小闭环 | ⬜ |
| P4 | Memory 写入治理 + 检索质量 | ⬜ |
| P5 | Multi-Agent 执行骨架 + Workspace 隔离 | ⬜ |
| P6 | 资源锁清理 / Run 持久化 / 共享 Memory | ⬜ |

单人开发的串行顺序建议 **P0→P1→P2→P3→P5→P6→P4**：P3 很小且立刻带来
`/memory` 的用户价值，P5 是最大的能力跃迁，P4 属于质量加固可以后置。

### P0 地基与可观测（已完成）

不产生用户可见功能，但后面每一期都依赖它。上下文文档 §10 说压缩比例
"应通过 Benchmark 调整，而不是硬编码为最终结论" —— 没有度量就没法 benchmark，
所以度量必须最先做。

- `TokenEstimator` 单一接口，全系统唯一估算入口
- `ContextProfile` 所有阈值可配置，一个常量都不许散落到别的模块
- `Message.category` + `Role.INTERNAL_CONTEXT`
- Trace ID 体系 + `Metrics`
- `/context` 读真实数字

**退出标准**：`/context` 打出正确的分项占用；代码里只有一个 estimator；
所有阈值可配置。✅

### P1 Evidence + Tool 结果治理 + ContextManager 外壳（已完成）

- `RawEventStore`（JSONL）+ `ArtifactStore`（文件，支持流式写）
- `ToolResultNormalizer`：tool 边界的有界化兜底
- `run_command` 流式落 artifact（**主防线**，见 §6.4）
- `ToolResultOffloader`：HOT / WARM / COLD 生命周期
- `ImagePayloadPruner`：裁 payload 但留描述
- `read_artifact` 工具：JIT 回读
- `ContextManager` 外壳（只挂 pruner）

**退出标准**：100K token 的 tool 输出不触发任何 history summary；artifact
可按 ref 回读；实测 token 下降幅度有数。✅（`tests/test_normalizer.py`、
`tests/test_run_command.py`）

### P2 Turn 模型 + Compaction 重写（全程最高风险）

挂载点：`context/compact/base.py` 的 `HistoryCompactor` Protocol。
现在装的是 `NullCompactor`，它诚实地报告"没压"。

要做的：

- `TurnStatus` 状态机（RUNNING 永不压缩）
- `HistoryChunker`：token-aware 且保持 Turn 原子性
- `HistoryMapSummarizer` → `TaskDelta`（结构化 JSON，用便宜快的模型）
- `TaskStateReducer` → `TaskCheckpoint`（**状态归并**，不是文本再总结，用主模型）
- Checkpoint + Delta 链（上下文文档 §24），避免 summary-of-summary 漂移
- §28 的降级阶梯：Map 失败保留原 chunk / Reduce 失败不替换 history /
  压完仍超预算走 emergency ladder / 最后才 `ContextOverflowError`

成本要提前算：150K 旧历史按 20K 切 = 8 次 Map + 1 次 Reduce。
`ModelConfig.map_model` 字段已经预留。整个 compaction 套
`profile.compaction_timeout_seconds`。

**退出标准**：上下文文档 §37.1–37.7 全绿，另加漂移回归 —— 同一条用户约束、
同一个架构决策、同一个 failed attempt，跨 3 次以上 compaction 后仍在
Checkpoint 里。

### P3 本地 Durable Memory 最小闭环

当前实现裁决：PROJECT scope SQLite store、Scope/Type/Source/Status、`EvidenceRef`、
FTS5 trigram + 两字 LIKE 回退、`/memory add|list|search|show|delete`、
`MEMORY.md` 人审投影，以及 request-local 的有界 Memory 注入。

- SQLite 使用标准库 `sqlite3 + asyncio.to_thread`，开启 WAL；不在 event loop 直接 I/O。
- 只有用户显式 `/memory add` 会写入，固定 PROJECT / USER_EXPLICIT / ACTIVE。
- SQLite 是唯一真源；`MEMORY.md` 更新失败不回滚 DB，下次按 revision 重建。
- Memory projection 只存在于一次 prepared request，不写回 ConversationHistory。
- Claude `memory_20250818` 是 provider tool contract，P3 不接入，保持 repository 中立。
- Session End candidate extraction 延至 P4，与 Judge/dedup/conflict/sensitive filter 同期，
  避免在治理能力缺失时自动升级长期知识。

**中文检索约束**：FTS5 使用 `tokenize='trigram'`；三字以上走 FTS，两字及更短
走转义后的参数化 LIKE。Tokenizer 选择后变更需要重建索引。

**退出标准**：Compact、`/clear` 和关闭重开 Session 均不影响 Durable Memory；
Memory 的 `EvidenceRef` 可回溯事件或 artifact；检索失败降级继续对话；注入不进入 History。

### P4 Memory 写入治理 + 检索质量

写入：LLM Judge、本地 dedup、语义冲突检测、SQLite 事务内 supersede + version、
敏感信息过滤、记忆 V2 §29 的优先级阶梯（显式规则永远压过 auto memory）。

读取：hybrid search、rerank、动态 token 预算、progressive disclosure
（`memory_get` / `evidence_get` 两级下钻）。

**退出标准**：§47.3–47.6 通过，尤其 47.5（assistant 猜测不能升级成高可信事实）。

### P5 Multi-Agent 执行骨架 + Workspace 隔离

`AgentRegistry` / `AgentRuntime`（含 reflection 循环）/ `StepScheduler` +
Semaphore / `TaskGraph` DAG / `LocalVerifier` + `GlobalVerifier` /
`MasterRuntime`。`ReActEngine`、`ToolExecutionManager`、`ContextManager`
都已经是 run-scoped 的，不需要改。

**Workspace 隔离必须和并行同期**，不能像并行文档那样把 worktree 排到第三期。
一旦开了 AgentRun 并行又允许写操作，两个 Agent 同时写同一个目录必然互相破坏。
来不及就先只让只读 Step 并行、写操作串行。

`StepScheduler` 不要按"波次"跑。并行文档 §10 的 `execute_ready_steps(steps)`
是一次一层、层间有 barrier，长短分支混在一起时会白等。改成 pending-set 循环：
每有一个 Step 完成就重算 ready 集合并立刻派发。

### P6 收尾

`ResourceLockManager` 锁表清理、Run 持久化与恢复、Multi-Agent 共享 Memory
（Worker 只产 `MemoryCandidate`，由 Supervisor 集中写）、专项 Agent 的
`MemoryProfile`。

---

## 4. 模块边界与依赖方向

```
cli/            REPL 与 /context /compact /memory /clear /metrics
  ↓
session.py      装配层：把所有组件接成一个可用会话
  ↓
runtime/        ReActEngine —— 只管 Agent Loop
  ↓         ↘
context/      tool/           两条互不依赖的支线
  ↓             ↓
evidence/     workspace/
  ↓             ↓
llm/  infra/    最底层，不依赖任何业务模块
```

硬约束：

- **`tool/` 不许 import `agent/`。** 工具执行需要的运行期信息通过
  `ExecutionScope`（`tool/execution_manager.py`）显式传入，不传 `AgentRun`。
  否则 tool ↔ agent 循环依赖。
- **`context/` 不许 import `tool/`。** 两者共用的文本有界化工具放在
  `infra/text.py` 这个中立位置。
- **`llm/` 不依赖任何业务模块。** Provider 适配层（`anthropic_client.py`）
  是唯一知道 Role 怎么映射到 Provider 协议的地方。

Provider 映射（`llm/anthropic_client.py`）：

| 内部 Role | Anthropic |
|---|---|
| `SYSTEM` | `system` 参数（不进 messages，因此**永不被压缩**） |
| `INTERNAL_CONTEXT` | user 消息，包 `<internal_context>` 标签 |
| `TOOL` | user 消息，装 tool_result blocks |
| `USER` / `ASSISTANT` | 原样 |

`system` 走独立参数而非 messages，正好落实记忆 V2 §13 的"System Prompt 不压"。
`INTERNAL_CONTEXT` 落实上下文文档 §22：压缩后注入的 Checkpoint 不是用户的新请求，
也不伪造一条 assistant 的"好的我已了解"。

---

## 5. 代码强制的六条不变式

这些不是文档里的建议，是有测试守着的。

### 5.1 `len(results) == len(calls)`，无条件成立

`ToolExecutionManager.execute_batch()`。

两份并行文档在这里有同一个真实 bug：`_execute_one` 末尾 `raise`，外层用
`asyncio.TaskGroup`（Java 版 `CompletableFuture::join`）。TaskGroup 在第一个
异常时取消所有兄弟任务，于是任一 tool 失败 = 整批被取消 = assistant 的 N 个
tool_call 只回来不到 N 个 tool_result —— 正是上下文文档 §37.1 明令禁止的
孤立 tool_call。

三层防护：

1. `_execute_one` 永不抛，异常一律转 `ToolResult.error`；
2. `gather(..., return_exceptions=True)` 再兜一道；
3. 最后从 `ToolRun` 记录里补洞，任何缺失的 call 都补一条结果。

捕获范围只能是 `except Exception`。写成 `except BaseException` 或裸 `except`
会吞掉 `CancelledError`（3.8+ 起它继承自 `BaseException`），取消就失效了。

测试：`tests/test_tool_protocol.py`。

### 5.2 tool 协议不存在孤立 tool_use / tool_result

`validate_tool_protocol()` 在每次 `ContextManager.prepare()` 结束时运行，
所以任何裁剪或压缩都不可能悄悄破坏协议。

两条配套约束：
- 一个 assistant turn 的**全部** tool_result 放进紧随其后的同一条消息
  （`Message.tool()`）；
- 协议 id 由 `ToolExecutionManager` **强制回填**（`call_id=call.id`），
  不依赖每个工具实现都写对。开发中真踩到过：工具误用 `tool_run_id`
  当协议 id，导致所有 tool_result 对不上 tool_use。

### 5.3 绝不静默有损删除

压缩失败或压完仍超 hard limit 时抛 `ContextOverflowError`，而不是
`messages[-N:]` 强删。P1 没有 Compactor，所以越过 hard limit 就是直接失败 ——
**这是刻意的**：宁可响亮失败，也不能在用户看不见的地方丢掉关键约束和架构决策。

`ConversationHistory.replace_messages()` 是改写历史的唯一入口，只允许等价改写
（同一条消息的裁剪版本）或压缩替换。

测试：`tests/test_context_manager.py::test_overflow_raises_instead_of_silent_truncation`。

### 5.4 无界输出永不进内存

分两层，缺一层都不行：

- **主防线**：可能产生无界输出的工具自己流式写 artifact。`run_command` 边读
  子进程输出边往 artifact 写，内存里只保留 head/tail 两段各 128KB 的预览。
- **兜底**：`ToolResultNormalizer` 对任何超限结果再补一刀。

只有兜底没有主防线是不行的 —— 300K 的输出在进 Normalizer 之前就已经把内存
吃掉了。这也是 §2.2 那个硬依赖的实现形态。

### 5.5 裁剪幂等

裁剪每轮都跑，已降级过的结果不能被反复再截断。`_degrade()` 保证输出落在
allowance 内，`ImagePayloadPruner` 跳过 `data is None` 的图片。

测试：`tests/test_context_manager.py::test_pruning_is_idempotent`。

### 5.6 Evidence 不丢

`RawEventStore.append_nowait()` 是同步入队（让 `ConversationHistory.append()`
不必是 async），退出前 `aclose()` 必须 drain 队列。记忆 V2 §44 要求的
"避免丢掉最后一轮候选 Memory"。

测试：`tests/test_event_store.py`。

---

## 6. asyncio 特有的实现决策

### 6.1 阻塞 event loop 比 Java 的线程池问题更尖锐

记忆 V2 §41 担心"长期 Memory I/O 占满 Agent 调度线程"，在 Java 里最坏是拖慢；
在 asyncio 里一次同步 `sqlite3` 查询会**卡住所有并发的 AgentRun**。没有隔离墙
兜底，所以纪律必须更严：

所有阻塞调用一律 `asyncio.to_thread` —— 文件读写、artifact 落盘、`grep` 的正则
遍历（CPU 活）、甚至 REPL 的 `input()`。P3 的 SQLite 走 `aiosqlite` 或
`to_thread`，开 WAL，单写者。P4 的 SimHash/MinHash 也要丢出去。

### 6.2 取消只留一套主机制

并行文档 §25 同时给了 `CancellationToken`（`asyncio.Event`）和 task cancel。
本实现以 `CancelledError` 为主（它在每个 await 点自动传播），Token 只用于两处：

1. 传给 subprocess，让工具能主动 kill 子进程；
2. 少数没有 await 点的长循环里做协作式检查。

`CancelledByUser` 是 `Exception` 子类，用于协作式取消 —— 它会被
`_execute_one` 捕获并转成 `ToolResult.cancelled`，产出完整记录；
而真正的 `asyncio.CancelledError` 直接杀掉整个 run。两条路径分得很清。

### 6.3 timeout 放最外层

`asyncio.timeout` 包在信号量和资源锁**外面**，而不是只包住工具执行本身。
否则一个拿不到锁的调用会永远挂住 —— timeout 必须同时约束排队时间。

### 6.4 JSONL 并发追加不加锁

`asyncio.Queue` + 单消费者任务 + 批量 flush。顺序天然有保证（单写者），
多个并发 AgentRun 不会互相插行，还能批量 fsync。见 §5.6。

### 6.5 TokenEstimator 双轨

`estimate()` 永远同步且便宜（启发式，结果缓存在 `Message.token_estimate` 上
—— 历史每轮全量重估是 O(n²)）。精确计数（`messages.count_tokens`）是网络调用，
只在压缩决策边界和校准时用。`CalibratedTokenEstimator` 把 exact/heuristic
的比值滑动平均进 `scale`，让启发式逐步收敛到真实值。

启发式系数：中文 ≈1 token/字，英文与代码 ≈3.6 字符/token，逐消息 8 token overhead。

### 6.6 资源锁排序获取

一批 tool 需要多把锁时锁 key 全局排序后按序获取，否则必然死锁
（并行文档 §20 自己也提了"需要考虑"）。持锁期间禁止调 LLM。
锁表清理留到 P6。

`SERIAL` 模式的处理是**整批退化为顺序执行**，而不是引入读写锁 ——
简单且显然正确。

---

## 7. 从并行文档里修掉的设计问题

除了 §5.1 那个 tool 协议 bug，还有两处：

### 7.1 Workspace 在合并前就被删了

并行文档 §8 的 `AgentRuntime.run()` 在 `finally` 里无条件
`workspace_manager.cleanup(workspace)`，而 §7 说"最终合并由 Master / Merge
阶段处理" —— 合并发生在 `run()` 返回之后。**成功路径上 worktree 会在被合并之前
就被删掉。**

修法：cleanup 的所有权上移到 `MasterRuntime`（合并之后），或者 cleanup 只释放
租约、由单独的 GC 回收。失败路径也别急着删，那是最有价值的 evidence。
已记在 `workspace/context.py` 的模块注释里，P5 实现时执行。

### 7.2 ReAct 迭代预算跨 reflection 轮不重置

并行文档 §9 的循环条件读 `run.context.react_iteration`，而 §8 的外层 reflection
循环会再次 `execute(run)`。第一轮用完预算后，后续每次 reflection 都立刻返回
"超过最大 ReAct 次数"，`LocalVerifier` 去验证一个 failed 结果，
`reflection_count` 继续加到上限。

本实现明确选择 **per-run 全局**语义：预算耗尽即终止，不再重进 ReAct，
并且用独立的 `RunStatus.MAX_ITERATIONS` 区分它和工具失败。
见 `agent/run.py::RunContext` 的注释。

---

## 8. 已知风险与未决问题

| 风险 | 现状 | 计划 |
|---|---|---|
| `run_command` 执行模型给出的任意 shell 命令，只挡了几条明显破坏性的命令，**没有**用户确认或沙箱 | 已在代码和 README 中标注 | gating 属于 CLI 层，需要在接真实模型做实际开发前补上 |
| 启发式 token 估算的系数是拍的 | `CalibratedTokenEstimator` 可以用精确计数校准，但默认没启用 | 接真实模型后跑一批真实会话校准 |
| `soft/hard/target` 三档比例（0.80 / 0.92 / 0.55）是照文档抄的 | 已全部可配置 | P2 完成后按真实 benchmark 调 |
| `TurnIdPartitioner` 对没打 `turn_id` 的消息用启发式补 | 单 Agent 下够用 | P2/P5 前换成纯 `turn_id` 驱动 |
| `ImagePayloadPruner` 裁掉 payload 后只留结构化占位，没有真的 vision 描述 | `summary` 字段已备好 | 接 vision summary 时只需填字段 |
| `_tests_from()` 在归一化后的摘要上提取测试计数，可能漏 | 影响 `AgentRunResult.tests` 的完整性，不影响正确性 | P2 让 Normalizer 把结构化计数写进 `metadata` |
| ResourceLockManager 锁表无限增长 | 长跑进程下是内存泄漏 | P6 |

---

## 9. 当前状态

```
30 tests passed
ruff check: All checks passed
pyright: 0 errors
```

跑起来：

```bash
conda activate mindcode
pip install -e ".[dev]"
python -m codeagent.cli.app --workspace .     # 无 API key 时走 Stub

pytest -q
ruff check .
pyright --pythonpath "$(which python)"        # pyright 需要显式指到 env 解释器
```

P2 的接入点只有一处：把 `session.py` 里传给 `ContextManager` 的
`NullCompactor` 换成真的 Compactor。`ReActEngine` 和 `AgentSession` 一行不改。






