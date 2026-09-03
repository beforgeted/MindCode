# CodeAgent 上下文管理与压缩机制设计文档

> 文档目标：重新梳理 CodeAgent 的上下文管理体系，以 `conversationHistory` 作为 LLM 上下文唯一事实源，统一 Token Budget、历史裁剪、工具结果压缩、会话压缩和长期记忆抽取机制，解决当前双短期状态源、摘要信息丢失、超长上下文成本过高以及多次压缩后的状态漂移问题。

---

# 1. 背景

当前 CodeAgent 中存在两套与会话上下文相关的数据结构：

```text
ConversationMemory
conversationHistory
```

其中：

```text
ConversationMemory
    ↓
ContextCompressor
    ↓
短期记忆压缩 / 长期事实抽取

conversationHistory
    ↓
ConversationHistoryCompactor
    ↓
真正作为 LLM messages 发送
```

早期设计假设：

```text
ConversationMemory
      ↓
构建下一轮 LLM Messages
      ↓
LLM
```

但实际 Agent 演进后采用：

```text
conversationHistory
      ↓
直接构建 LLM Request
      ↓
LLM
```

因此形成了两个并行的短期上下文状态源。

这会产生几个问题：

1. `ConversationMemory` 被压缩后，并不会显著减少真实 LLM 请求的 Token。
2. `ConversationMemory` 与 `conversationHistory` 可能出现状态不一致。
3. 两套压缩逻辑增加系统复杂度。
4. 真正重要的 `ConversationHistoryCompactor` 目前反而采用较简单的单次摘要。
5. 超过 `MAX_SUMMARY_INPUT_CHARS = 60000` 的历史可能在摘要过程中被截断。
6. 多次 Summary-of-Summary 会逐渐发生信息漂移。

因此，需要重新设计统一的 Context Management 架构。

---

# 2. 设计目标

新的上下文系统需要满足以下目标。

## 2.1 唯一上下文事实源

规定：

```text
conversationHistory
```

为 Agent 当前会话状态的唯一事实源。

其他 Memory、Summary、Index 均由其派生，不再与其平行维护一份完整会话。

---

## 2.2 控制 LLM Input Token

上下文管理必须真正作用于：

```text
最终发送给 LLM 的 Messages
```

而不仅仅作用于内部 Memory。

---

## 2.3 长任务状态连续

压缩后必须尽量保留 Coding Agent 最重要的信息：

```text
当前任务
用户约束
架构决策
已完成工作
修改文件
测试状态
失败尝试
当前问题
下一步计划
```

目标不是简单保存“聊过什么”，而是保存：

```text
Agent 当前工作状态
```

---

## 2.4 避免协议被切断

压缩时不能破坏：

```text
assistant tool_call
       ↓
tool_result
```

以及未来的：

```text
SubAgent
Parallel Tool Calls
Cancellation
Interrupted Run
```

因此需要按照完整 Conversation Turn 进行裁剪，而不是简单按照消息数量切分。

---

## 2.5 降低压缩频率

在真正执行 LLM Summary 之前，优先：

```text
删除图片 Payload
压缩旧 Tool Result
移除低价值中间信息
```

只有这些措施仍然无法满足 Token Budget 时，才执行 History Compaction。

---

# 3. 总体架构

新的整体架构如下：

```text
                         User Message
                              │
                              ▼
                 ┌────────────────────────┐
                 │   ConversationHistory  │
                 │    会话唯一事实源       │
                 └────────────┬───────────┘
                              │
                              │ before LLM call
                              ▼
                    ┌──────────────────┐
                    │  ContextManager  │
                    └────────┬─────────┘
                             │
        ┌────────────────────┼────────────────────┐
        │                    │                    │
        ▼                    ▼                    ▼
 ImagePayloadPruner   ToolResultPruner      TokenBudget
                                                   │
                                                   ▼
                                         ContextBudgetPredictor
                                                   │
                                  ┌────────────────┴───────────────┐
                                  │                                │
                             Budget OK                       Need Compact
                                  │                                │
                                  │                                ▼
                                  │                      HistoryCompactor
                                  │                                │
                                  │                        Turn Chunking
                                  │                                │
                                  │                         Map Summaries
                                  │                                │
                                  │                         State Reducer
                                  │                                │
                                  │                                ▼
                                  │                       TaskCheckpoint
                                  │                                │
                                  └────────────────┬───────────────┘
                                                   ▼
                                            LLM Request
                                                   │
                                                   ▼
                                                  LLM
                                                   │
                                                   ▼
                                              Tool Calls
                                                   │
                                                   ▼
                                       ConversationHistory


ConversationHistory
        │
        │ async / opportunistic
        ▼
MemoryExtractor
        │
        ▼
LongTermMemory
```

核心原则：

> **ConversationHistory 管当前任务，TaskCheckpoint 管压缩后的当前状态，LongTermMemory 管跨 Session 的稳定知识。**

三者职责不能混淆。

---

# 4. 上下文分层

整个 Context 建议分成四层。

```text
┌────────────────────────────────────┐
│ Layer 1：Persistent Context        │
│                                    │
│ System Prompt                      │
│ AGENTS.md / Project Instructions   │
│ Skills                             │
│ Project Memory                     │
└────────────────────────────────────┘

┌────────────────────────────────────┐
│ Layer 2：Task Checkpoint           │
│                                    │
│ Goal                               │
│ Constraints                        │
│ Decisions                          │
│ Completed Work                     │
│ Files                              │
│ Tests                              │
│ Open Issues                        │
│ Next Steps                         │
└────────────────────────────────────┘

┌────────────────────────────────────┐
│ Layer 3：Recent Conversation       │
│                                    │
│ 最近 N 个完整 Turn                 │
│ 最近工具调用                        │
│ 最近文件操作                        │
└────────────────────────────────────┘

┌────────────────────────────────────┐
│ Layer 4：Ephemeral Payload         │
│                                    │
│ Tool stdout                        │
│ 大文件内容                          │
│ 图片 Payload                       │
│ 构建日志                            │
└────────────────────────────────────┘
```

优先级：

```text
Persistent Context
        >
Task Checkpoint
        >
Recent Conversation
        >
Historical Tool Payload
```

当 Token 不足时，应从最下面开始释放空间。

---

# 5. ConversationHistory

## 5.1 定位

`conversationHistory` 是当前 Session 唯一完整消息源。

包含：

```text
system
user
assistant
tool
```

以及必要时扩展的内部消息元信息。

其他组件不能独立维护另一套完整 Conversation。

---

## 5.2 写入流程

所有新的 Agent 行为统一写入：

```java
conversationHistory.append(message);
```

之后可发布事件：

```java
contextEventBus.publish(
    new ConversationMessageAdded(message)
);
```

Memory、日志、统计等能力通过事件派生。

即：

```text
             ConversationHistory
                    │
           ┌────────┼─────────┐
           ▼        ▼         ▼
        Memory   Metrics    Trace
```

而不是：

```text
           Agent
        ┌────┴────┐
        ▼         ▼
    History     Memory

两边分别写
```

---

# 6. ConversationTurn

当前压缩通过倒数第 N 个 `user` 消息确定切分点，这个方案已经可以避免多数 Tool Call 被切断。

但长期建议抽象：

```java
public record ConversationTurn(
    String turnId,
    List<LlmClient.Message> messages,
    TurnStatus status
) {}
```

其中：

```java
enum TurnStatus {
    RUNNING,
    COMPLETED,
    CANCELLED,
    FAILED
}
```

一个 Turn 可以包含：

```text
User
 │
Assistant
 │
├─ ToolCall A
│      ↓
│  ToolResult A
│
├─ ToolCall B
│      ↓
│  ToolResult B
│
└─ Assistant
```

因此压缩策略变成：

```text
Old Complete Turns
        ↓
可压缩

Recent Complete Turns
        ↓
保留

Running Turn
        ↓
永远不压缩
```

这样能够天然兼容：

```text
Parallel Tool Calls
SubAgent
Cancellation
Tool Exception
Streaming interruption
```

---

# 7. ContextManager

建议新增统一入口：

```java
public final class ContextManager {

    private final TokenBudget tokenBudget;
    private final ImagePayloadPruner imagePruner;
    private final ToolResultPruner toolResultPruner;
    private final ConversationHistoryCompactor historyCompactor;
    private final ContextBudgetPredictor budgetPredictor;

    public ContextPreparationResult prepare(
        List<Message> history,
        ContextProfile profile
    ) {
        ...
    }
}
```

Agent 主循环不再自己散落：

```text
图片裁剪
token estimate
compact
memory compress
```

统一为：

```java
while (!cancelled) {

    ContextPreparationResult context =
        contextManager.prepare(
            conversationHistory,
            contextProfile
        );

    LlmResponse response =
        llmClient.chat(context.messages());

    ...
}
```

这样上下文策略从 `Agent` 中解耦。

---

# 8. 上下文处理流水线

每轮调用 LLM 前执行：

```text
conversationHistory
        │
        ▼
① Prune Historical Images
        │
        ▼
② Prune Historical Tool Results
        │
        ▼
③ Estimate Current Tokens
        │
        ▼
④ Predict Next Round Token Risk
        │
        ├── Safe
        │      ↓
        │   Build Request
        │
        └── Unsafe
               ↓
⑤ Compact Old Turns
               │
               ▼
⑥ Verify Target Budget
               │
               ▼
⑦ Build LLM Request
```

重要的是：

> **Compaction 是最后一道昂贵防线，而不是第一道。**

---

# 9. TokenBudget

当前 TokenBudget 使用启发式估算：

```text
Text Content
Image Content
Tool Arguments
Message Overhead
```

这个方案可以继续使用。

建议统一所有 Token 估算入口，不再让：

```text
MemoryEntry.estimateTokens()
TokenBudget.estimateMessagesTokens()
```

分别维护两套规则。

统一：

```java
public interface TokenEstimator {

    int estimate(Message message);

    int estimate(List<Message> messages);
}
```

Provider 如果未来支持精确 token counter：

```text
AnthropicTokenEstimator
OpenAITokenEstimator
HeuristicTokenEstimator
```

可以按 Provider 替换。

---

# 10. ContextProfile

当前：

```text
summaryReserve = min(20000, max(1000, window / 4))
buffer         = min(13000, max(1000, window / 8))

trigger =
window
- summaryReserve
- buffer
```

对于 200K：

```text
200000
- 20000
- 13000
=
167000
```

约 83.5%。

该策略在常规 Context Window 下合理，但随着 Window 变成：

```text
1M
2M
```

固定 33K Reserve 会导致：

```text
1M → 96.7%
```

才触发压缩。

风险包括：

```text
高 Prefill 成本
高 Latency
突然出现大 Tool Result 时可能冲穿窗口
```

因此新版本推荐采用：

```text
Soft Trigger
Hard Trigger
Target After Compression
```

示意：

```java
softTrigger = window * 0.80;
hardTrigger = window * 0.92;
targetAfterCompression = window * 0.55;
```

具体比例应通过 Benchmark 调整，而不是硬编码为最终结论。

---

# 11. Token Risk Prediction

不能只计算：

```text
currentTokens
```

还要计算：

```text
下一轮预计还需要多少 Token。
```

建议：

```java
predictedTokens =
      currentInputTokens
    + expectedToolBurst
    + reservedOutputTokens
    + safetyMargin;
```

例如：

```text
Context Window          200K

当前历史               150K
预计 Tool Burst          20K
最大模型输出             16K
Safety Margin             5K
----------------------------
Predicted               191K
```

虽然当前：

```text
150K < 167K
```

但下一轮风险已经非常高，应提前处理。

因此压缩条件调整为：

```java
boolean shouldCompact =
       currentTokens >= softTrigger
    || predictedTokens >= hardTrigger;
```

---

# 12. ToolResultPruner

Coding Agent 最容易膨胀的往往不是对话，而是：

```text
read_file
grep
mvn test
gradle test
npm test
git diff
compiler output
```

因此应该在 History Compaction 前增加专门的 Tool Result Pruning。

---

## 12.1 工具结果生命周期

工具结果可以分成：

```text
HOT
WARM
COLD
```

### HOT

最近 Turn 使用中的工具结果。

保持完整。

### WARM

稍早但可能还需要引用。

进行轻度压缩。

### COLD

很老的工具输出。

仅保留结构化摘要或引用。

---

## 12.2 示例

原始：

```text
Tool:
mvn test

[23000 token Maven output]
```

压缩后：

```text
ToolResultSummary

tool: execute_command
command: mvn test
exitCode: 1

tests:
  passed: 127
  failed: 1

failures:
  - PaymentConcurrentTest

keyErrors:
  - expected balance=80
  - actual balance=70

originalResultRef:
  tool-run-83
```

LLM 真正需要的往往是：

```text
执行了什么
成功还是失败
关键输出是什么
```

而不是 2000 行日志。

---

# 13. 工具结果可恢复性

如果底层 ToolRun 已经持久化：

```text
ToolRun
    id
    toolName
    args
    rawOutput
```

那么 History 中甚至不需要永久保存完整结果。

可以：

```text
ConversationHistory
    ↓
ToolResultSummary
    ↓
toolRunId
```

需要重新查看时：

```text
Agent
  ↓
ToolResultRepository
  ↓
load(toolRunId)
```

形成：

```text
Context = Working Set
Storage = Full Evidence
```

这会比把所有历史信息永久塞进 Context Window 更合理。

---

# 14. ImagePayloadPruner

当前：

```java
pruneHistoricalImagePayloads()
```

会移除历史图片 Payload。

该方向是合理的，因为图片成本较高。

但推荐：

```text
Image Payload
     ↓
Vision Result / Description
     ↓
删除历史 Binary/Base64 Payload
```

例如：

```text
ImageSummary:
用户上传了一张架构图。

关键内容：
- Master Agent
- Worker Agent
- ReAct 模式
- Worker 可并行
- Tool 可并行
```

然后：

```text
图片本体
   ↓
仅当前 Turn 保留

图片摘要
   ↓
后续 Turn 保留
```

否则完全删除后，模型可能不知道之前图片提供了什么重要信息。

---

# 15. ConversationHistoryCompactor

这是整个系统真正负责减少 LLM Input Token 的核心组件。

推荐重构为：

```java
public final class ConversationHistoryCompactor {

    private final TurnPartitioner turnPartitioner;
    private final HistoryChunker chunker;
    private final HistoryMapSummarizer mapSummarizer;
    private final TaskStateReducer stateReducer;

}
```

压缩流程：

```text
History
   │
   ▼
TurnPartitioner
   │
   ├─ Old Turns
   └─ Recent Turns
          │
          ▼
Old Turns
   │
   ▼
Token-aware Chunking
   │
   ├─ Chunk 1
   ├─ Chunk 2
   ├─ Chunk 3
   └─ Chunk N
          │
          ▼
      Map Summary
          │
          ▼
      State Reducer
          │
          ▼
     TaskCheckpoint
          │
          ▼
Checkpoint + Recent Turns
```

---

# 16. 为什么不能再使用 60000 字符直接截断

当前：

```text
old messages
     ↓
序列化
     ↓
超过 60000 chars
     ↓
截断
```

存在永久信息丢失风险。

例如：

```text
第 1~20 轮
用户关键约束

第 21~50 轮
大量工具输出

第 51~80 轮
代码修改

第 81 轮
compact
```

如果：

```text
serializedHistory > 60000 chars
```

任何区域都有可能没有进入 Summary。

改进后必须保证：

> 每一段被淘汰的历史至少经过一次 Map Summary。

即：

```text
所有 old turns
       ↓
全部参与某个 chunk
       ↓
全部形成中间摘要
```

---

# 17. Token-aware Chunking

旧 ContextCompressor 使用：

```text
每 5 条消息一个 Chunk
```

对于 Agent 不够稳定。

因为：

```text
1 message = 30 tokens

也可能

1 message = 20000 tokens
```

新方案使用：

```text
MAX_MAP_INPUT_TOKENS
```

例如：

```java
int maxChunkTokens = 20_000;
```

算法：

```text
Turn 1  3K
Turn 2  8K
Turn 3  6K
----------------
Chunk 1 = 17K

Turn 4  15K
----------------
Chunk 2 = 15K
```

始终保持 Turn 原子性。

---

# 18. Map Phase

Map 阶段不应该生成普通聊天摘要。

推荐输出结构化 Delta：

```text
TaskDelta

User Requirements:
- ...

Decisions:
- ...

Completed:
- ...

Files Changed:
- ...

Tests:
- ...

Failed Attempts:
- ...

Open Issues:
- ...

Important Evidence:
- ...
```

只提取当前 Chunk 内发生的新状态变化。

---

# 19. Reduce Phase

Reduce 的目标不是：

```text
summary1
summary2
summary3
↓
生成一段更短的话
```

而应该是：

```text
Existing Task State
        +
New Task Deltas
        ↓
Updated Task State
```

即：

```text
State Reduction
```

而非：

```text
Text Summarization
```

---

# 20. TaskCheckpoint

最终 Compact 后保存统一结构：

```java
public record TaskCheckpoint(

    String goal,

    List<String> userConstraints,

    List<Decision> decisions,

    List<String> completedWork,

    List<FileState> files,

    List<TestState> tests,

    List<FailedAttempt> failedAttempts,

    List<String> openIssues,

    List<String> nextSteps

) {}
```

示例：

```text
[Task]
修复支付模块并发扣款问题。

[User Constraints]
- 不修改 public API
- Java 17
- 不引入 Redis

[Decisions]
- 使用数据库 optimistic locking
- version 冲突最大重试 3 次
- 不使用 JVM synchronized

[Completed]
- PaymentEntity 增加 version
- PaymentMapper 增加 version 条件
- PaymentService 增加重试框架

[Files]
MODIFIED:
- PaymentEntity.java
- PaymentMapper.xml
- PaymentService.java

[Test Status]
PASS:
- PaymentServiceTest

FAIL:
- PaymentConcurrentTest
  expected=80
  actual=70

[Failed Attempts]
- synchronized：无法解决多实例部署并发

[Open Issues]
- affectedRows == 0 后没有重新读取 version

[Next Steps]
1. 修改 retry loop
2. 重新运行 PaymentConcurrentTest
3. 运行全部测试
```

---

# 21. Checkpoint 与 Recent Turns

Compact 后 LLM 上下文：

```text
System Prompt
      │
Project Instructions
      │
Relevant Long-Term Memory
      │
TaskCheckpoint
      │
Recent Turn 1
      │
Recent Turn 2
      │
Recent Turn 3
```

其中：

```text
Checkpoint
=
之前发生了什么以及任务当前在哪里

Recent Turns
=
刚刚具体发生了什么
```

两者结合能够同时保持：

```text
长期连续性
+
局部细节
```

---

# 22. 不再注入虚假的 Assistant Message

当前：

```text
user:
[已压缩历史摘要]

assistant:
好的，我已了解之前的上下文，请继续。
```

第二条 Assistant 消息实际上没有真实发生。

新设计建议引入内部 Context Message：

```java
Message.internalContext(...)
```

最终根据 Provider 协议映射。

逻辑表示：

```text
<task_checkpoint>
...
</task_checkpoint>
```

必须明确：

```text
这不是用户的新请求
这是系统维护的历史状态
```

如果 Provider 不支持独立内部角色，可以映射到 user message：

```text
<internal_context>
以下是系统维护的历史任务状态，
不是新的用户指令：

...
</internal_context>
```

无需伪造 Assistant acknowledgement。

---

# 23. Compression Target

不能仅定义：

```text
什么时候开始压缩
```

还应该定义：

```text
压缩完以后希望剩多少
```

例如：

```text
Context Window = 200K

Soft Trigger              160K
Hard Trigger              184K
Target After Compression  105K
```

于是：

```text
165K
 ↓
Compact
 ↓
100~110K
```

提供足够 Headroom。

否则：

```text
165K
 ↓
Compact
 ↓
150K
 ↓
几轮以后
 ↓
165K
 ↓
再 Compact
```

会频繁产生 Summary-of-Summary。

---

# 24. Checkpoint + Delta 机制

进一步避免摘要漂移，可以保留：

```text
Base Checkpoint
+
Recent Delta
```

例如：

```text
Checkpoint #3
   │
   ├─ Delta 4
   ├─ Delta 5
   └─ Recent Turns
```

达到一定规模以后：

```text
Checkpoint #3
+ Delta 4
+ Delta 5
      ↓
Checkpoint #4
```

而不是每次：

```text
整个历史
   ↓
重新总结
```

---

# 25. LongTermMemory

长期 Memory 与 Context Compaction 必须明确分开。

```text
Compaction
=
保持当前任务连续工作

LongTermMemory
=
跨 Session 保存稳定知识
```

长期记忆适合保存：

```text
用户稳定偏好
项目技术约束
项目架构规则
稳定环境信息
明确确认过的工程规范
```

不适合保存：

```text
这次任务下一步
刚刚某个测试失败
临时修改
模型推测
当前 TODO
```

---

# 26. MemoryCandidate

当前通过：

```text
EPHEMERAL_FACT_PREFIXES
SPECULATION_CUES
```

过滤长期 Memory，可以作为第一层规则，但建议逐步升级成结构化 Memory Candidate。

```java
public record MemoryCandidate(

    MemoryType type,

    MemoryScope scope,

    MemorySource source,

    String content,

    double confidence,

    Instant expiresAt

) {}
```

例如：

```java
enum MemoryType {
    USER_PREFERENCE,
    PROJECT_CONVENTION,
    ARCHITECTURE_DECISION,
    STABLE_FACT
}
```

```java
enum MemoryScope {
    GLOBAL,
    PROJECT,
    SESSION
}
```

```java
enum MemorySource {
    USER_EXPLICIT,
    TOOL_VERIFIED,
    ASSISTANT_INFERRED
}
```

默认策略：

```text
USER_EXPLICIT
     ↓
高可信，可持久化

TOOL_VERIFIED
     ↓
项目事实，可持久化

ASSISTANT_INFERRED
     ↓
默认禁止长期持久化
```

---

# 27. ContextCompressor 的去留

现有：

```text
ContextCompressor
```

不建议继续承担“控制 LLM 上下文大小”的职责。

建议逐步重构为：

```text
MemorySummarizer
+
MemoryExtractor
```

其职责：

```text
ConversationHistory
       ↓
提取检索型 Memory
       ↓
提取长期 Fact
```

而真正的上下文压缩全部交给：

```text
ContextManager
    +
ConversationHistoryCompactor
```

最终：

```text
ContextCompressor
```

可以被删除或重命名。

---

# 28. 降级策略

上下文压缩属于高风险操作。

必须遵循：

> 宁可暂时保留更多 Context，也不能为了压缩而静默丢失关键状态。

---

## 28.1 Map Summary 失败

某一 Chunk Summary 失败：

```text
Retry 1
 ↓
仍失败
 ↓
保留该 Chunk 原始消息
```

不能直接：

```text
substring(0, 200)
```

作为真正 History 的最终压缩结果。

---

## 28.2 Reduce 失败

```text
Map Summaries
      ↓
Reduce Failed
```

则：

```text
不替换原始 History
```

本轮继续使用 Pruned History。

---

## 28.3 Compact 后仍超预算

执行 Emergency Strategy：

```text
① 再压旧 Tool Result
② 减少 Recent Turn 数
③ 进行第二级 Map-Reduce
④ 最终仍无法满足 → 返回 ContextOverflowException
```

不能静默从头部：

```java
history.subList(...)
```

强删。

---

# 29. 特殊情况：单条工具输出超大

可能存在：

```text
Context Window = 200K

single tool output = 300K
```

这类情况不能等到 HistoryCompactor 才解决。

Tool 层应直接限制：

```text
Tool
 ↓
Raw Result Store
 ↓
Tool Result Normalizer
 ↓
Preview / Summary
 ↓
ConversationHistory
```

例如：

```text
Raw output 10MB
     ↓
保存文件
     ↓
History 只写：

output too large
lines: 230000
artifact: xxx
preview:
...
```

---

# 30. `/compact`

手动：

```text
/compact
```

仍然保留。

语义变为：

```text
立即执行 ContextManager.forceCompact()
```

区别：

```text
自动 Compact
保留默认 recent turns

手动 /compact
更积极压缩，可以只保留 1 个最近 Turn
```

也支持：

```text
/compact 保留数据库修改、测试失败和所有架构决策
```

作为本轮 compaction 的额外重点。

---

# 31. `/context`

建议 `/context` 输出：

```text
Context Window
200,000

Current Estimated Input
124,800

Usage
62.4%

Soft Compact Trigger
160,000

Hard Safety Threshold
184,000

Estimated Next Round
151,000

Breakdown

System / Instructions     8,200
Task Checkpoint           4,100
Recent Conversation     38,500
Tool Results            61,000
Memory Injection         5,000
Other                    8,000

Last Compaction
Turn #83

Current Checkpoint
#4
```

这样用户可以真正观察：

```text
Token 到底花在哪里
为什么触发 Compact
```

---

# 32. 建议的数据结构

可以逐渐形成：

```text
context/
├── ContextManager.java
├── ContextProfile.java
├── ContextBudget.java
├── ContextBudgetPredictor.java
├── TokenEstimator.java
│
├── history/
│   ├── ConversationHistory.java
│   ├── ConversationTurn.java
│   ├── TurnPartitioner.java
│   └── ConversationHistoryCompactor.java
│
├── compact/
│   ├── HistoryChunker.java
│   ├── HistoryMapSummarizer.java
│   ├── TaskStateReducer.java
│   ├── TaskCheckpoint.java
│   └── TaskDelta.java
│
├── prune/
│   ├── ImagePayloadPruner.java
│   ├── ToolResultPruner.java
│   └── ToolResultSummary.java
│
└── memory/
    ├── MemoryExtractor.java
    ├── MemoryCandidate.java
    └── LongTermMemory.java
```

职责会比现在清晰很多。

---

# 33. Agent 主循环

重构前概念上类似：

```java
while (...) {

    pruneHistoricalImagePayloads();

    maybeCompactHistory();

    List<Message> messages = conversationHistory;

    LlmResponse response =
        llmClient.chat(messages);

    ...
}
```

重构后：

```java
while (!cancellationToken.isCancelled()) {

    ContextPreparationResult prepared =
        contextManager.prepare(
            conversationHistory,
            contextProfile
        );

    LlmResponse response =
        llmClient.chat(
            prepared.messages()
        );

    ConversationTurnResult result =
        executeResponse(response);

    conversationHistory.append(result.messages());
}
```

Agent 只负责：

```text
Agent Loop
```

ContextManager 负责：

```text
Context Engineering
```

---

# 34. ContextManager 伪代码

```java
public ContextPreparationResult prepare(
        ConversationHistory history,
        ContextProfile profile) {

    // 1. 首先处理高成本的历史图片
    imagePayloadPruner.prune(history);

    // 2. 清理低价值历史工具结果
    toolResultPruner.prune(history, profile);

    // 3. 当前占用
    int currentTokens =
        tokenEstimator.estimate(history.messages());

    // 4. 预测下一轮风险
    ContextBudgetPrediction prediction =
        budgetPredictor.predict(
            history,
            currentTokens,
            profile
        );

    // 5. 判断是否需要压缩
    if (prediction.shouldCompact()) {

        historyCompactor.compact(
            history,
            profile
        );
    }

    // 6. 再次验证
    int finalTokens =
        tokenEstimator.estimate(history.messages());

    if (finalTokens > profile.hardLimit()) {
        throw new ContextOverflowException(...);
    }

    return new ContextPreparationResult(
        history.buildLlmMessages(),
        finalTokens
    );
}
```

---

# 35. Compactor 伪代码

```java
public boolean compact(
        ConversationHistory history,
        ContextProfile profile) {

    List<ConversationTurn> turns =
        turnPartitioner.partition(history);

    List<ConversationTurn> recent =
        selectRecentTurns(
            turns,
            profile.retainRecentRounds()
        );

    List<ConversationTurn> old =
        turnsBefore(recent);

    if (old.isEmpty()) {
        return false;
    }

    List<List<ConversationTurn>> chunks =
        chunker.chunkByTokens(
            old,
            profile.mapChunkTokens()
        );

    List<TaskDelta> deltas = new ArrayList<>();

    for (List<ConversationTurn> chunk : chunks) {

        TaskDelta delta =
            mapSummarizer.summarize(chunk);

        deltas.add(delta);
    }

    TaskCheckpoint checkpoint =
        stateReducer.reduce(
            history.currentCheckpoint(),
            deltas
        );

    history.replaceOldTurnsWith(
        checkpoint,
        recent
    );

    return true;
}
```

---

# 36. 可观测性

每次 Context 准备建议记录：

```text
context.input_tokens_before
context.input_tokens_after_prune
context.input_tokens_after_compact

context.tool_tokens_removed
context.image_tokens_removed

context.compaction_triggered
context.compaction_duration

context.map_chunks
context.checkpoint_tokens

context.compaction_count
```

并重点关注：

```text
平均多少 Turn compact 一次
Compact 后平均释放多少 Token
多少请求出现连续 Compact
多少 Compact 失败
Task completion 是否因 Compact 下降
```

---

# 37. 测试设计

## 37.1 Tool Protocol

构造：

```text
assistant → tool_call
tool → result
```

触发压缩后验证：

```text
不存在孤立 tool result
不存在孤立 tool call
```

---

## 37.2 Parallel Tool Calls

```text
Assistant
 ├── Tool A
 ├── Tool B
 └── Tool C
```

确保整个 Tool Batch 保持完整。

---

## 37.3 User Constraint Retention

第 1 轮：

```text
不能修改 public API
```

运行几十轮 + 多次 compact。

最终验证 Checkpoint 仍存在：

```text
不能修改 public API
```

---

## 37.4 Decision Retention

早期：

```text
明确决定使用 optimistic lock
```

多次 Compact 后：

```text
Decision 必须仍然保留
```

---

## 37.5 Failed Attempt

早期尝试：

```text
使用 synchronized
```

随后证明不可行。

压缩后必须保存：

```text
Failed Attempt:
synchronized 不适用于多实例
```

避免 Agent 再走一遍错误路线。

---

## 37.6 Large Tool Output

生成：

```text
100K Token Tool Result
```

验证：

```text
优先由 ToolResultPruner 处理
而不是立刻触发整段 History Summary
```

---

## 37.7 Summary Failure

Mock LLM Summary 失败。

验证：

```text
History 不发生有损替换
```

---

# 38. 迁移方案

不建议一次性删除当前 Memory 系统。

可以分阶段演进。

---

## Phase 1：修复核心风险

首先完成：

```text
ConversationHistoryCompactor
        ↓
Token-aware Map-Reduce
```

移除：

```text
MAX_SUMMARY_INPUT_CHARS
导致的直接历史截断
```

并升级 Summary 为 TaskCheckpoint。

---

## Phase 2：增加 Tool Result Pruning

实现：

```text
Historical Tool Result
        ↓
ToolResultSummary
```

降低真实 Compact 频率。

---

## Phase 3：ContextManager 收口

把：

```text
Token 判断
Image Pruning
Tool Pruning
Compaction
Context Building
```

统一搬入：

```text
ContextManager
```

Agent 不再了解具体压缩策略。

---

## Phase 4：弱化 ConversationMemory

不再把：

```text
ConversationMemory
```

当作另一份短期对话。

改为：

```text
Derived Memory
```

主要服务：

```text
Memory Retrieval
Long-Term Fact Extraction
```

---

## Phase 5：Turn Model

引入：

```text
ConversationTurn
```

替代当前简单的 user message boundary。

为：

```text
Parallel Tool
Multi-Agent
Cancellation
```

提供更稳定的协议边界。

---

# 39. 第一版建议落地范围

不需要一开始全部实现。

V1 推荐只做四件事情：

```text
1. conversationHistory 作为唯一 Context Source

2. ConversationHistoryCompactor
   从 60K 截断 + 单次 Summary
   改成 Token-aware Map-Reduce

3. Summary
   从 1~3 段自然语言
   改成 TaskCheckpoint

4. conversationHistory 增加 ToolResultPruner
```

这四项完成后，就已经能够解决目前最大的几个问题：

```text
双状态源
历史直接截断
摘要状态不完整
Tool Result 大量占用 Token
```

---

# 40. 最终设计原则

整个上下文系统可以浓缩成五条原则。

### 1. History 是唯一事实源

```text
conversationHistory
```

必须成为当前 Session 唯一真实会话状态。

---

### 2. 先裁剪，再摘要

顺序：

```text
Image Pruning
      ↓
Tool Result Pruning
      ↓
History Compaction
```

不要一遇到 Token 压力就调用 LLM 总结。

---

### 3. 保存状态，而不是保存聊天

Coding Agent 的 Compact 应该生成：

```text
Task Checkpoint
```

而不是普通：

```text
Conversation Summary
```

---

### 4. Context 是 Working Set，不是 Storage

```text
Context Window
=
当前模型解决问题真正需要的信息

Storage
=
完整工具结果、运行历史、文件证据
```

不要试图把所有东西永久留在 Context Window 中。

---

### 5. Compression 必须可控且可观测

必须知道：

```text
为什么触发
压了什么
释放多少 Token
留下什么
压缩是否失败
```

不能让 Context Compaction 成为一个不可观察的黑盒。

---

# 41. 最终架构总结

最终推荐演进为：

```text
                        ┌──────────────────┐
                        │ Conversation     │
                        │ History          │
                        │ 唯一会话事实源    │
                        └────────┬─────────┘
                                 │
                                 ▼
                         Context Manager
                                 │
                  ┌──────────────┼──────────────┐
                  │              │              │
                  ▼              ▼              ▼
              Images           Tools        Token Risk
               Prune           Prune        Prediction
                  │              │              │
                  └──────────────┼──────────────┘
                                 │
                                 ▼
                         Need Compaction?
                                 │
                     ┌───────────┴──────────┐
                     │                      │
                    No                     Yes
                     │                      │
                     │                      ▼
                     │                Old Complete Turns
                     │                      │
                     │                Token Chunking
                     │                      │
                     │                  Map Phase
                     │                      │
                     │                 Task Deltas
                     │                      │
                     │                 State Reduce
                     │                      │
                     │                      ▼
                     │                TaskCheckpoint
                     │                      │
                     └───────────┬──────────┘
                                 │
                                 ▼
                         Recent Complete Turns
                                 │
                                 ▼
                               LLM
                                 │
                                 ▼
                         Tool / SubAgent
                                 │
                                 ▼
                       ConversationHistory


ConversationHistory
         │
         ▼
   MemoryExtractor
         │
         ▼
  LongTermMemory
```

最终形成三套职责完全不同的数据：

```text
ConversationHistory
=
当前 Session 的真实事件流

TaskCheckpoint
=
当前任务经过压缩后的工作状态

LongTermMemory
=
跨 Session 的稳定知识
```

这三者分离后，整个 Agent 的上下文体系会比当前“两套 Conversation 并行压缩”的设计清晰很多，也更适合后续继续扩展 Multi-Agent、长期运行 Agent 和更大的 Context Window。