# CodeAgent Memory System V2 设计文档

> 目标：在现有 `conversationHistory + ContextManager + TaskCheckpoint` 的上下文机制上，补齐长期 Memory、原始 Evidence、Multi-Agent Scope 和可观测性，形成适用于本地 Coding Agent 的完整信息管理系统。
>
> 设计参考：
> - 保留 CodeAgent 当前 Context Engineering 优势；
> - 借鉴得物 MultiAgent 的 Scope、Long Memory Retrieval、LLM Judge、Dedup、Conflict 机制；
> - 借鉴 Claude Code 的 CLAUDE.md / Auto Memory / JSONL Transcript / JIT Retrieval / SubAgent Context Isolation。

---

# 1. 设计结论

V2 不再把“记忆系统”理解成一个 `MemoryManager + Vector DB`，而拆成三个互相独立的 Plane：

```text
                 Agent Information System

       ┌────────────────┬────────────────┐
       │                │                │
       ▼                ▼                ▼
 Runtime Context    Durable Memory    Raw Evidence
       │                │                │
 ContextManager      MemoryManager      EventStore
       │                │                │
       └────────────────┼────────────────┘
                        │
                        ▼
                       LLM
```

三条核心定义：

```text
Context
= 当前模型这一轮使用的 Working Set

Memory
= 经过筛选、去重、可检索、可更新的长期知识

Evidence
= 原始完整历史，是 Memory 和 Summary 的可追溯来源
```

另外：

```text
Skill
= 可执行过程 / SOP
```

Skill 与 Memory 分离。

---

# 2. 设计目标

V2 需要同时解决六类问题。

## 2.1 Context 不爆窗口

保持现有能力并升级：

```text
Token Budget
Image Pruning
Tool Result Offload
History Compaction
TaskCheckpoint
Recent Complete Turns
```

## 2.2 Compact 后任务状态不漂移

Coding Agent 应保留：

```text
Goal
Constraints
Decisions
Files
Tests
Failed Attempts
Open Issues
Next Steps
```

而不是只有普通 Conversation Summary。

## 2.3 跨 Session 能复用稳定知识

例如：

```text
项目约束
用户稳定偏好
已经确认的架构决策
历史失败经验
构建与测试习惯
外部资料入口
```

## 2.4 Memory 不能污染

必须避免：

```text
临时任务写成长久事实
Assistant 猜测写成事实
重复 Memory 无限累积
过期架构仍被召回
互相冲突的 Memory 同时注入
```

## 2.5 Multi-Agent 不能互相污染 Working State

Worker 的大量 Tool History 不应直接进入 Master Context。

## 2.6 所有重要 Memory 必须可审计

至少支持：

```text
查看
来源追踪
编辑
删除
失效
查看冲突
```

---

# 3. 非目标

V2 第一阶段不追求：

- 企业级多租户 RBAC；
- Redis + MySQL 双活架构；
- 独立远程 Memory Service；
- 复杂分布式一致性；
- 自动生成完整 Skill；
- 无限层级的知识图谱。

原因：CodeAgent 当前定位为本地 CLI Coding Agent，优先选择简单、可调试、可演进的实现。

---

# 4. 总体架构

```text
                                  ┌──────────────────────┐
                                  │    RawEventStore     │
                                  │                      │
                                  │ Message              │
                                  │ AgentRun             │
                                  │ ToolRun              │
                                  │ ToolResult           │
                                  │ Diff                 │
                                  │ TestLog              │
                                  │ Artifact             │
                                  └──────────┬───────────┘
                                             │
                       ┌─────────────────────┼──────────────────────┐
                       │                                            │
                       ▼                                            ▼
            ┌──────────────────────┐                      MemoryExtractor
            │ ConversationHistory  │                             │
            │ Runtime Working Set  │                             ▼
            └──────────┬───────────┘                      MemoryCandidate
                       │                                            │
                       ▼                                            ▼
                 ContextManager                                MemoryJudge
                       │                                            │
          ┌────────────┼─────────────┐                              ▼
          │            │             │                           Dedup
          ▼            ▼             ▼                              │
   ImagePruner   ToolResultOffload  BudgetPredictor                  ▼
          │            │             │                           Conflict
          └────────────┼─────────────┘                              │
                       │                                            ▼
                       ▼                                        MemoryStore
                 Need Compact?                                       │
                       │                                  ┌───────────┼────────────┐
                       ▼                                  │           │            │
               HistoryCompactor                          ▼           ▼            ▼
                       │                              UserMemory  ProjectMemory AgentMemory
                       ▼
                TaskCheckpoint
                       +
                Recent Complete Turns
                       +
                Relevant Memory
                       │
                       ▼
                      LLM
```

---

# 5. 数据职责边界

## 5.1 RawEventStore：最终历史事实源

保存原则：

```text
完整
不可因为 Context Compact 而丢失
可追踪
可按 Run / Session 查询
```

建议事件：

```text
USER_MESSAGE
ASSISTANT_MESSAGE
TOOL_CALL
TOOL_RESULT
AGENT_RUN_STARTED
AGENT_RUN_FINISHED
FILE_CHANGED
TEST_RESULT
CHECKPOINT_CREATED
MEMORY_CREATED
MEMORY_UPDATED
```

它回答：

> “当时到底发生了什么？”

---

## 5.2 ConversationHistory：当前 Runtime Context Source

现有设计中的：

```text
conversationHistory
```

继续作为：

> **构建下一次 LLM Request 的唯一 Runtime 消息源。**

但不再把它称为整个系统的“唯一事实源”。

更准确的定义：

```text
RawEventStore
= Durable Historical Truth

ConversationHistory
= Active LLM Working History
```

ConversationHistory 可以被：

```text
Prune
Offload
Compact
Replace With Checkpoint
```

而 RawEventStore 不受这些操作影响。

---

## 5.3 Durable Memory：长期知识

Memory 不保存完整 Conversation。

它只保存：

```text
未来很可能再次有用
难以从代码直接重新推导
具有稳定性
有明确 Scope
有 Evidence 来源
```

的高价值知识。

---

# 6. Memory Scope 设计

不直接照搬四层，而使用显式 Scope。

```java
public enum MemoryScope {
    RUN,
    SESSION,
    USER,
    PROJECT,
    AGENT
}
```

## RUN

生命周期：单次 AgentRun。

内容：

```text
当前 Plan
ReAct 状态
Tool Call 状态
临时变量
Reflexion 中间状态
```

默认只存在内存，不进入长期搜索。

## SESSION

生命周期：当前 Conversation。

内容：

```text
TaskCheckpoint
Session Summary
未完成任务状态
```

用于 Resume。

## USER

跨 Project。

适合：

```text
用户明确的稳定偏好
全局工作习惯
个人工具偏好
```

必须非常克制。

## PROJECT

Repository / Workspace 级共享知识。

这是 Coding Agent 最重要的长期 Scope。

适合：

```text
稳定架构决策
项目约束
构建知识
难以直接从代码推导的历史背景
已验证的重要坑
```

## AGENT

Agent Definition 专属经验。

例如某个专项 Worker：

```text
DatabaseAgent
FrontendAgent
SecurityAgent
```

可以维护自己的专项 Memory。

普通 Worker 默认不应拥有永久私有 Memory，避免 Agent 数量增长导致 Memory 碎片化。

---

# 7. Memory Type 设计

Scope 与 Type 正交。

```java
public enum MemoryType {
    FACT,
    PREFERENCE,
    CONSTRAINT,
    DECISION,
    FAILURE,
    WORKFLOW,
    TOOL_INSIGHT,
    REFERENCE
}
```

示例：

```text
scope   = PROJECT
type    = CONSTRAINT
content = 项目固定使用 Java 17
```

```text
scope   = PROJECT
type    = FAILURE
content = synchronized 不能解决多实例并发问题
```

```text
scope   = USER
type    = PREFERENCE
content = 用户偏好解释原理后再进入源码
```

---

# 8. Memory 数据模型

推荐 V2 数据结构：

```java
public record MemoryEntry(
    String id,
    MemoryScope scope,
    String scopeId,
    MemoryType type,
    String content,
    MemorySource source,
    double confidence,
    int importance,
    MemoryStatus status,
    List<EvidenceRef> evidenceRefs,
    List<String> tags,
    Instant createdAt,
    Instant updatedAt,
    Instant expiresAt,
    long version
) {}
```

MemorySource：

```java
public enum MemorySource {
    USER_EXPLICIT,
    TOOL_VERIFIED,
    ASSISTANT_DERIVED
}
```

MemoryStatus：

```java
public enum MemoryStatus {
    ACTIVE,
    SUPERSEDED,
    EXPIRED,
    DELETED
}
```

---

# 9. EvidenceRef

所有重要 Memory 都应该尽可能有来源。

```java
public record EvidenceRef(
    EvidenceType type,
    String eventId,
    String sessionId,
    String agentRunId,
    String toolRunId,
    String artifactPath
) {}
```

使用效果：

```text
Memory:
项目固定 Java 17

Evidence:
session-12 / user-message-33
```

用户可以：

```text
/memory show <id>
```

看到它是从哪里来的。

这能显著降低“模型偷偷记错”的风险。

---

# 10. Context Plane 设计

ContextManager 继续作为 LLM 调用前的统一入口。

```java
public final class ContextManager {
    private final TokenEstimator tokenEstimator;
    private final ContextBudgetPredictor budgetPredictor;
    private final ImagePayloadPruner imagePruner;
    private final ToolResultOffloader toolResultOffloader;
    private final ConversationHistoryCompactor historyCompactor;
    private final MemoryRetriever memoryRetriever;

    public ContextPreparationResult prepare(
        AgentContext agentContext,
        ConversationHistory history,
        ContextProfile profile
    ) {
        ...
    }
}
```

---

# 11. LLM 调用前完整流程

```text
ConversationHistory
      │
      ▼
1. prune historical images
      │
      ▼
2. offload old large tool results
      │
      ▼
3. estimate current tokens
      │
      ▼
4. predict next-round risk
      │
      ├─ safe
      │
      └─ unsafe
           │
           ▼
5. compact old complete turns
           │
           ▼
6. load relevant durable memory
           │
           ▼
7. allocate memory token budget
           │
           ▼
8. assemble final context
           │
           ▼
9. verify hard limit
           │
           ▼
          LLM
```

顺序很重要。

先：

```text
Prune / Offload / Compact
```

再：

```text
Memory Retrieval
```

否则 Context 已经很满时仍然盲目注入长期 Memory，会增加压力。

---

# 12. ToolResult 从“截断”升级成 Offload

当前 `addToolResult()` 有固定字符截断思想。

V2 改为：

```text
Raw Tool Result
      │
      ├──────────────> ArtifactStore
      │                  完整保存
      │
      ▼
ToolResultSummary
      │
      ▼
ConversationHistory
```

数据结构：

```java
public record ToolResultArtifact(
    String toolRunId,
    String toolName,
    String rawArtifactPath,
    String summary,
    int rawTokenEstimate,
    int summaryTokenEstimate,
    int exitCode,
    Instant createdAt
) {}
```

例如：

```text
mvn test 原始输出：25K tokens
```

History 只保留：

```text
tool: execute_command
command: mvn test
exitCode: 1
passed: 127
failed:
- PaymentConcurrentTest
keyError:
- expected=80 actual=70
artifactRef: toolrun://892/result
```

需要原始信息时再 JIT 读取 Artifact。

---

# 13. History Compaction 设计

保留现有最重要的三个特点：

```text
System Prompt 不压
按完整 Turn 边界切分
Recent Complete Turns 保留
```

但旧 History 摘要升级为：

```text
Token-aware Map
        ↓
TaskDelta
        ↓
State Reduce
        ↓
TaskCheckpoint
```

不能再使用“超过 60000 字符直接截断旧 History”的方式。

原则：

> 每一段最终被移出 Runtime Context 的历史，至少要被某个 Map Chunk 正式处理过。

---

# 14. TaskCheckpoint Schema

```java
public record TaskCheckpoint(
    String goal,
    List<String> constraints,
    List<DecisionState> decisions,
    List<String> completedWork,
    List<FileState> files,
    List<TestState> tests,
    List<FailedAttempt> failedAttempts,
    List<String> openIssues,
    List<String> nextSteps,
    List<EvidenceRef> evidenceRefs,
    Instant updatedAt
) {}
```

Checkpoint 不是 Long-Term Memory。

区别：

```text
TaskCheckpoint
= 当前 Session 的任务状态

Project Memory
= 跨 Session 的稳定知识
```

Session 结束后，MemoryExtractor 可以从 Checkpoint 和新事件中提取候选长期 Memory，但不能直接把整个 Checkpoint 持久化到 Project Memory。

---

# 15. Context Budget 设计

保留 `ContextProfile(maxContextWindow)`，但 V2 将预算拆成：

```text
hardWindow
softTrigger
targetAfterCompression
outputReserve
safetyMargin
memoryBudget
expectedToolBurst
```

核心公式：

```text
predictedTokens =
    currentInputTokens
  + expectedToolBurst
  + outputReserve
  + safetyMargin
```

触发条件：

```text
current >= softTrigger
OR
predicted >= hardWindow
```

而不是只看：

```text
currentTokens / maxWindow
```

---

# 16. Durable Memory Read Pipeline

请求进入后，长期 Memory 不应该直接 TopK 全塞 Prompt。

推荐：

```text
User Query + Task State
        │
        ▼
Scope Resolver
        │
        ▼
Candidate Search
        │
        ├─ Keyword
        ├─ Vector
        └─ Metadata Filter
        │
        ▼
Hybrid Merge
        │
        ▼
Dedup / Rerank
        │
        ▼
Relevance Filter
        │
        ▼
Token Budget Allocator
        │
        ▼
Memory Context
```

---

# 17. Scope Retrieval 策略

默认 Coding Task：

```text
PROJECT
>
SESSION
>
AGENT
>
USER
```

这和得物 User-first 的业务场景不同。

Coding Agent 中，Project Memory 往往比 User Profile 更重要。

建议默认：

```text
PROJECT 允许占主要预算
USER 只有相关时才注入
AGENT 只对专项 Agent 注入
SESSION 由 TaskCheckpoint 负责为主
```

不要固定：

```text
User Memory = 60%
```

而是动态分配。

---

# 18. Memory Token Budget

推荐：

```text
availableMemoryBudget =
    maxContextWindow
  - systemTokens
  - checkpointTokens
  - recentTurnTokens
  - activeToolTokens
  - outputReserve
  - safetyMargin
```

然后：

```text
actualMemoryBudget = min(
    profile.maxMemoryInjectionTokens,
    availableMemoryBudget
)
```

最终按照相关性，而非固定 Scope 百分比分配。

---

# 19. Claude Code 式 Progressive Disclosure

Memory Store 中不要只有“一堆完整正文”。

可以设计两级表示：

```text
MemoryIndexEntry
    ↓
FullMemoryEntry
    ↓
Evidence
```

例如 Context 中先注入：

```text
[M-33] Project Decision:
AgentDefinition 无状态，AgentRun 保存运行状态。
```

模型如果需要更多信息，可以调用：

```text
memory_get(M-33)
```

得到：

```text
完整内容
时间
来源
EvidenceRef
相关 Memory
```

再需要时：

```text
evidence_get(eventId)
```

回到原始消息或 Tool Result。

实现：

```text
Index -> Memory -> Evidence
```

这就是 Agent 版 JIT Retrieval。

---

# 20. Durable Memory Write Pipeline

长期 Memory 写入建议只在以下事件触发：

```text
Session End
Task Completed
Explicit User "remember"
重要决策确认
Periodic Checkpoint
```

默认不在每一条消息后都调用 LLM Judge，避免成本过高。

完整流程：

```text
New Events Since Last Memory Pass
        │
        ▼
MemoryCandidateExtractor
        │
        ▼
Rule Pre-filter
        │
        ▼
LLM MemoryJudge
        │
        ▼
Normalize Candidate
        │
        ▼
Local Dedup
        │
        ▼
Semantic Conflict Detection
        │
        ▼
Conflict Resolution
        │
        ▼
Persist New Version
        │
        ▼
Supersede Old Version
```

---

# 21. MemoryCandidate

```java
public record MemoryCandidate(
    MemoryScope scope,
    String scopeId,
    MemoryType type,
    String content,
    MemorySource source,
    double confidence,
    int importance,
    List<EvidenceRef> evidenceRefs,
    String reason
) {}
```

其中 `reason` 用于调试：

```text
为什么认为值得长期保存？
```

生产环境可以不注入 LLM Context，但应保留在审计数据中。

---

# 22. Memory Judge Prompt 原则

Judge 不应该问：

```text
“请总结这段对话。”
```

而应该判断：

```text
这条信息未来是否仍然有用？
是否可直接从代码重新得到？
是否是用户明确事实？
是否只是当前任务临时状态？
属于哪个 Scope？
属于什么 Type？
可信度如何？
重要程度如何？
```

推荐输出结构化 JSON：

```json
{
  "shouldRemember": true,
  "scope": "PROJECT",
  "type": "DECISION",
  "content": "AgentDefinition 保持无状态，执行状态放入 AgentRun",
  "importance": 9,
  "confidence": 0.98
}
```

---

# 23. 规则层先做 Cheap Filter

调用 LLM Judge 前先做廉价规则过滤。

例如直接排除：

```text
纯 Tool stdout
纯堆栈日志
一次性 TODO
明显临时命令
空消息
重复的 Assistant acknowledgment
```

但不要继续使用大量脆弱的中文前缀规则决定最终 Memory。

规则层只负责：

```text
明显不值得记 -> 直接丢
其他 -> 交给 Judge
```

---

# 24. Local Dedup

第一层使用确定性算法，成本低。

建议：

```text
Normalized Exact Match
Contains
SimHash / MinHash（可选）
Token Jaccard
Short Text Levenshtein
```

如果明显重复：

```text
skip LLM conflict check
```

减少成本。

---

# 25. Semantic Conflict Detection

只有潜在相关 Memory 才进入冲突判断。

例如：

Existing：

```text
项目使用 Java 17
```

New：

```text
项目已升级到 Java 21
```

这不是重复，而是：

```text
Supersede
```

冲突结果建议：

```java
public enum ConflictAction {
    KEEP_BOTH,
    SKIP_NEW,
    SUPERSEDE_OLD,
    MERGE,
    REQUIRE_USER_CONFIRMATION
}
```

特别重要的约束冲突可以要求用户确认，而不是让 LLM 自己决定。

---

# 26. 一致性策略

本地 V1 推荐使用 SQLite Transaction，而不是完全照搬得物“先写后删 best-effort”。

更新流程：

```text
BEGIN
  insert new version
  mark old version SUPERSEDED
COMMIT
```

使用：

```text
version
updatedAt
status
```

如果后续 Memory Store 变成远程服务，再引入：

```text
write-new-first
retry
outbox
compensation
```

---

# 27. Storage 设计

针对本地 CLI，推荐：

```text
~/.codeagent/
└── projects/
    └── <project-id>/
        ├── sessions/
        │   ├── <session-id>.jsonl
        │   └── ...
        │
        ├── artifacts/
        │   ├── tool-results/
        │   ├── diffs/
        │   └── tests/
        │
        ├── memory.db
        │
        └── memory/
            ├── MEMORY.md
            └── topics/
```

建议采用：

```text
JSONL
= Raw session transcript

Artifact Files
= 超大 Tool Output / Diff / Test Log

SQLite
= Memory metadata + FTS + relations

Markdown
= 用户可审计的高价值 Project Memory 索引
```

这是一种混合方案：

```text
Claude Code 的文件可审计性
+
得物式结构化 Memory Governance
```

---

# 28. 为什么还需要 MEMORY.md

即使有 SQLite，也建议维护人可读索引：

```text
memory/MEMORY.md
```

例如：

```markdown
# Project Memory Index

- [M-12] Java 17 is the fixed runtime version
- [M-31] AgentDefinition is stateless; state belongs to AgentRun
- [M-44] Agent and Tool schedulers must use separate pools
```

作用：

```text
人能看
Git / 文件工具能看
Agent 可 JIT 读取
数据库坏了也容易排查
```

但数据库仍是结构化 Memory 的正式存储。

---

# 29. Project Rules 与 Memory 分离

长期规则分两类。

## Explicit Rules

用户或团队明确写入：

```text
AGENTS.md / CODEAGENT.md / rules/
```

属于：

```text
Instruction
```

优先级高。

## Auto Memory

Agent 从使用中自动学习：

```text
MemoryStore
```

属于：

```text
Learned Knowledge
```

不能让 Auto Memory 覆盖显式规则。

优先级建议：

```text
System Policy
>
Project Explicit Rules
>
User Explicit Current Instruction
>
Verified Memory
>
Assistant-derived Memory
```

---

# 30. Skill 与 Memory 分离

现有 SKILL 系统继续独立。

```text
Memory
= 知识

Skill
= 行动 SOP
```

Memory 可引用 Skill：

```java
record MemoryEntry(...) {
    List<String> relatedSkillIds;
}
```

例如：

```text
Memory:
本项目新增 MCP Tool 需要遵守内部注册约定。

relatedSkill:
mcp-tool-development
```

LLM 需要执行时再加载 Skill Body。

---

# 31. Multi-Agent Context 模型

```text
                         Shared Project Memory
                                  ▲
                                  │ read
                  ┌───────────────┼──────────────┐
                  │               │              │
               Master         Worker A       Worker B
                  │               │              │
             Master Run      Worker Run     Worker Run
                  │               │              │
             Own Context     Own Context     Own Context
                                  │              │
                                  ▼              ▼
                           AgentRunResult  AgentRunResult
                                  │              │
                                  └──────┬───────┘
                                         ▼
                                       Master
```

原则：

```text
共享 Project Memory
不共享 Working Memory
不直接共享完整 conversationHistory
```

---

# 32. Worker 输出协议

Worker 不返回几十 K Tool History，而返回结构化结果。

```java
public record AgentRunResult(
    RunStatus status,
    String summary,
    List<String> decisions,
    List<FileState> files,
    List<TestState> tests,
    List<String> openIssues,
    List<EvidenceRef> evidenceRefs,
    List<MemoryCandidate> memoryCandidates
) {}
```

Master 只把高信号结果写入自己的 Context。

Tool / Agent 细节仍可通过 EvidenceRef 下钻。

---

# 33. Worker Memory 写权限

默认：

```text
Worker
可以读取 PROJECT Memory
可以维护 RUN State
可以生成 MemoryCandidate
不能直接写 PROJECT Memory
```

正式写入：

```text
Worker Candidates
      │
      ▼
Supervisor / MemoryManager
      │
      ▼
Judge / Dedup / Conflict
      │
      ▼
Project Memory
```

这样避免多个 Worker 并行修改同一长期事实。

---

# 34. Memory Retrieval 与 Multi-Agent

不同 Worker 可以有不同检索 Profile。

例如：

```text
CodingWorker:
PROJECT + WORKFLOW + DECISION

TestWorker:
PROJECT + FAILURE + TOOL_INSIGHT

SecurityWorker:
PROJECT + CONSTRAINT + SECURITY-RELATED FACT
```

通过 AgentDefinition 配置：

```java
public record MemoryProfile(
    Set<MemoryScope> readableScopes,
    Set<MemoryType> preferredTypes,
    int maxInjectionTokens
) {}
```

---

# 35. `/context` 可观测性

建议扩展为：

```text
Context Window
200,000

Current Input
124,800

Predicted Next Round
151,000

Soft Trigger
160,000

Hard Limit
184,000

Breakdown
--------------------------------
System / Rules          8,200
TaskCheckpoint          4,100
Recent Turns           38,500
Active Tool Results    41,000
Injected Memory         7,000
Other                  26,000

Memory Retrieval
--------------------------------
Project Memory           5 items / 5,800 tokens
User Memory              1 item  /   600 tokens
Agent Memory             1 item  /   600 tokens

Last Compact
Turn 83
Released 61,200 tokens
```

---

# 36. `/memory` 可审计性

命令建议：

```text
/memory list
/memory search <query>
/memory show <id>
/memory evidence <id>
/memory edit <id>
/memory delete <id>
/memory forget <id>
/memory conflicts
```

展示：

```text
M-42
scope: PROJECT
type: DECISION
importance: 9
source: USER_EXPLICIT
confidence: 1.0
status: ACTIVE

content:
AgentDefinition 必须保持无状态。

Evidence:
session-12/message-33

Created:
2026-08-01
```

这点直接吸收 Claude Code Markdown Memory 的“用户能看懂、能改”的优势。

---

# 37. `/compact`

保留：

```text
/compact
/compact <focus>
```

手动 Compact 只作用于：

```text
Runtime ConversationHistory
```

不会：

```text
删除 Raw Events
删除 Durable Memory
```

用户指定 Focus 时：

```text
/compact 保留数据库迁移决策和测试失败
```

作为 State Reducer 的额外权重。

---

# 38. `/clear`

建议语义：

```text
结束当前 Runtime Context
创建新 Session Context
```

但：

```text
Raw Session 保留
Project / User Memory 保留
```

即：

```text
Clear Context != Forget Memory
```

如果需要真正忘记：

```text
/memory forget
```

---

# 39. Memory 安全与污染控制

## 39.1 Prompt Injection 不应自动进入长期 Memory

来自：

```text
网页
MCP Resource
工具输出
第三方文档
```

的信息默认 Source 不应是 USER_EXPLICIT。

MemoryJudge 必须知道数据来源。

## 39.2 用户显式“记住”仍需 Scope

例如：

```text
“记住这个项目必须 Java17”
```

应解析为：

```text
PROJECT / CONSTRAINT
```

而不是全局 USER Memory。

## 39.3 敏感信息过滤

例如：

```text
password
token
secret key
credential
private key
```

默认禁止自动长期持久化。

---

# 40. 失败与降级策略

## Memory Retrieval 失败

```text
warning
+
继续对话
```

不能因为长期 Memory 服务异常导致 Agent 完全不可用。

## Memory Judge 失败

```text
本轮不写长期 Memory
```

而不是用高风险猜测兜底。

## Compaction 失败

```text
保持原始 Runtime History
```

不能静默有损删除。

## Artifact 写入失败

超大 Tool Result 不允许直接全部塞回 Context。

应：

```text
返回 ToolResultStorageException
或只保留安全 Preview 并明确标记 evidence unavailable
```

---

# 41. 线程池与并发

继续保持职责隔离：

```text
AgentScheduler
ToolExecutor
MemoryLoadExecutor
MemoryWriteExecutor
```

禁止全部共用：

```text
ForkJoinPool.commonPool
```

原因：

```text
长期 Memory I/O
不应该占满 Agent 调度线程
```

Memory Read 可并行：

```text
Session Load
+
Project/User Memory Search
```

但最终 Context 组装必须等待相关 Future 完成或明确 timeout 降级。

---

# 42. 建议 Java 包结构

```text
memory/
├── MemoryManager.java
├── MemoryStore.java
├── MemoryEntry.java
├── MemoryCandidate.java
├── MemoryScope.java
├── MemoryType.java
├── MemorySource.java
├── MemoryStatus.java
│
├── extract/
│   ├── MemoryCandidateExtractor.java
│   └── MemoryJudge.java
│
├── retrieve/
│   ├── MemoryRetriever.java
│   ├── HybridMemorySearch.java
│   ├── MemoryReranker.java
│   └── MemoryBudgetAllocator.java
│
├── conflict/
│   ├── MemoryDeduplicator.java
│   ├── MemoryConflictDetector.java
│   └── MemoryConflictResolver.java
│
└── persistence/
    ├── SqliteMemoryStore.java
    └── MemoryRepository.java

context/
├── ContextManager.java
├── ContextProfile.java
├── ContextBudgetPredictor.java
├── TokenEstimator.java
│
├── history/
│   ├── ConversationHistory.java
│   ├── ConversationTurn.java
│   └── ConversationHistoryCompactor.java
│
├── checkpoint/
│   ├── TaskCheckpoint.java
│   ├── TaskDelta.java
│   └── TaskStateReducer.java
│
└── prune/
    ├── ImagePayloadPruner.java
    └── ToolResultOffloader.java

evidence/
├── RawEventStore.java
├── JsonlEventStore.java
├── EvidenceRef.java
├── ArtifactStore.java
└── FileArtifactStore.java
```

---

# 43. 核心接口

## MemoryManager

```java
public interface MemoryManager {

    List<MemoryEntry> retrieve(
        MemoryQuery query,
        MemoryBudget budget
    );

    List<MemoryCandidate> extract(
        MemoryExtractionContext context
    );

    PersistResult persist(
        List<MemoryCandidate> candidates
    );
}
```

## RawEventStore

```java
public interface RawEventStore {

    void append(AgentEvent event);

    List<AgentEvent> query(
        String sessionId,
        EventQuery query
    );
}
```

## ArtifactStore

```java
public interface ArtifactStore {

    ArtifactRef save(
        String type,
        byte[] content,
        ArtifactMetadata metadata
    );

    Artifact load(ArtifactRef ref);
}
```

---

# 44. Session End 流程

```text
Agent Session End
      │
      ▼
Flush Raw Events
      │
      ▼
Build New Event Delta
      │
      ▼
MemoryCandidateExtractor
      │
      ▼
MemoryJudge
      │
      ▼
Dedup + Conflict
      │
      ▼
Persist Memory
      │
      ▼
Update MEMORY.md Index
      │
      ▼
Save Final TaskCheckpoint
```

Memory 写入可以异步，但应用退出前应：

```text
flush 或有可靠 journal
```

避免直接丢失最后一轮候选 Memory。

---

# 45. Agent 启动流程

```text
Start Agent
   │
   ▼
Resolve Project ID
   │
   ▼
Load Explicit Rules
   │
   ▼
Load Small Memory Index
   │
   ▼
Resume Session?
   ├─ Yes -> Load Session Checkpoint + Recent Turns
   └─ No  -> Fresh Session
   │
   ▼
User Prompt
   │
   ▼
Search Relevant Memory JIT
   │
   ▼
ContextManager.prepare()
   │
   ▼
LLM
```

这里吸收 Claude Code 的关键思想：

> 启动时只加载少量高优先级持久信息，详细 Memory 按 Query JIT 搜索。

---

# 46. 可观测指标

Context：

```text
context.tokens.before
context.tokens.after_prune
context.tokens.after_compact
context.tokens.memory_injected
context.compaction.count
context.compaction.duration
context.compaction.released_tokens
```

Memory Retrieval：

```text
memory.search.latency
memory.search.candidates
memory.search.selected
memory.search.tokens
memory.search.scope_distribution
```

Memory Write：

```text
memory.candidates.extracted
memory.candidates.accepted
memory.dedup.dropped
memory.conflict.count
memory.persist.success
memory.persist.failure
```

质量：

```text
memory.user_deleted
memory.user_corrected
memory.stale_recalled
memory.false_recall
```

最后四个指标尤其重要，因为 Memory System 的核心风险不是“搜不到”，而是：

```text
记错 + 错误召回
```

---

# 47. 测试设计

## 47.1 Context Compact 不影响 Durable Memory

1. 创建 Project Memory；
2. 运行长对话触发 Compact；
3. 验证 Memory 仍可正常检索。

## 47.2 Compact 后可回溯 Evidence

1. 历史 Tool Result 被 Offload；
2. History 只剩 Summary；
3. 通过 EvidenceRef 加载原始 Tool Result。

## 47.3 用户约束长期保持

用户：

```text
项目不能升级 Java21
```

经过多 Session 后仍能正确召回，并且 Source 为 USER_EXPLICIT。

## 47.4 临时任务不能污染长期 Memory

用户：

```text
这次先帮我临时把日志调成 debug
```

Session End 后不应形成长期 Project Constraint。

## 47.5 Assistant 猜测不能升级成高可信事实

Assistant：

```text
可能项目历史上因为 XXX 才这样设计
```

不能自动成为 ACTIVE / high-confidence Project Fact。

## 47.6 冲突更新

Existing：

```text
Java17
```

User Explicit New：

```text
项目现在正式升级 Java21
```

结果：

```text
Java17 -> SUPERSEDED
Java21 -> ACTIVE
```

## 47.7 Worker 不污染 Master Context

Worker 产生 80K Tool History，Master 最终只接收到结构化 AgentRunResult。

## 47.8 Worker 不可直接覆盖 Shared Memory

Worker 只能产生 MemoryCandidate，最终由 MemoryManager 写入。

---

# 48. 迁移方案

## Phase 1：先修 Runtime Context

保留当前总体结构，完成：

```text
ConversationHistoryCompactor
-> Token-aware Map-Reduce

普通 Summary
-> TaskCheckpoint

Tool Result 截断
-> Artifact Offload
```

同时引入 RawEventStore。

这是最高优先级。

## Phase 2：建立 Local Durable Memory

实现：

```text
SQLite MemoryStore
MemoryScope / Type
EvidenceRef
Keyword Search
/memory list/show/delete
```

先不做 Vector Search。

## Phase 3：Memory Write Governance

增加：

```text
MemoryCandidate
LLM Judge
Local Dedup
Conflict Detection
Version / Supersede
```

## Phase 4：Hybrid Retrieval

增加：

```text
Embedding
Hybrid Search
Rerank
Dynamic Token Budget
Progressive Disclosure
```

## Phase 5：Multi-Agent Shared Memory

增加：

```text
Agent MemoryProfile
Worker MemoryCandidate
Supervisor Central Write
Agent private auto memory（仅专项 Agent）
```

---

# 49. 第一版 MVP 范围

如果不想把 V2 一次做太大，建议 MVP 只做：

```text
1. RawEventStore(JSONL)
2. ToolResult Artifact Offload
3. TaskCheckpoint
4. SQLite ProjectMemory
5. Scope + Type + EvidenceRef
6. Keyword Search
7. /memory list/show/delete
8. Session End 的简单 Candidate Extraction
```

暂缓：

```text
Embedding
LLM Conflict Resolution
复杂 User Memory
Agent Private Memory
远程 Memory Service
```

这样已经能获得大部分架构收益。

---

# 50. 最终设计原则

## 原则 1：Context 不是 Storage

```text
Context
只保存当前推理高价值 Working Set
```

## 原则 2：Memory 不是 Conversation Archive

Memory 应该：

```text
少
稳
高信号
可更新
可追踪
```

## 原则 3：Evidence 永远优先于 Summary

Summary 有损，因此关键 Memory 应有 EvidenceRef。

## 原则 4：先 JIT Retrieval，再考虑更大 Prompt

能：

```text
search/read/load on demand
```

就不要全部预加载。

## 原则 5：显式规则高于自动 Memory

用户和项目规则不能被自动 Memory 悄悄覆盖。

## 原则 6：Memory 写入比 Memory 检索更危险

搜索不到通常只是“不知道”。

记错了则会导致：

```text
错误知识长期污染未来所有 Session
```

因此写入必须有 Judge、Source、Dedup、Conflict 和 Audit。

## 原则 7：Multi-Agent 共享知识，不共享全部过程

Worker 独立 Context，通过 Structured Result + EvidenceRef 和 Master 通信。

---

# 51. 最终一句话架构

CodeAgent V2 的 Memory System 最终不是一个“记忆数据库”，而是：

```text
             Context Engineering
                    +
              Durable Memory
                    +
              Raw Evidence
                    +
          Multi-Agent Scope Control
```

其中：

```text
ContextManager
负责“这一轮模型看什么”；

MemoryManager
负责“以后应该记住什么”；

RawEventStore
负责“当时到底发生了什么”；

Skill System
负责“遇到问题应该怎么做”。
```

这四者边界清晰后，后续无论接更大的 Context Window、向量数据库、远程 Memory Service 还是更多 SubAgent，都不需要推翻核心架构。
