# CodeAgent Multi-Agent 并行架构设计方案（Java 版）

> 目标：实现一套支持 **Plan + Multi-Agent 并行 + ReAct + Tool 并行 + Reflection / Verification + Workspace 隔离** 的 Coding Agent 架构。  
> 推荐环境：**Java 21+**。如果项目仍为 Java 17，可将虚拟线程替换为固定线程池。

---

# 1. 总体设计原则

核心不是“池化 Agent 对象”，而是：

```text
长期共享：
- AgentDefinition
- AgentRuntime
- Tool
- ToolRegistry
- LLMClient
- Planner / Scheduler

每次执行新建：
- MasterRun
- AgentRun
- RunContext
- ToolRun
- WorkspaceContext
```

核心抽象：

```text
AgentDefinition = Agent 是谁、会什么
AgentRun        = Agent 这一次执行
AgentRuntime    = Agent 怎么跑
Tool            = 工具能力定义/实现
ToolRun         = 某次工具调用
Workspace       = 某次 AgentRun 的代码工作区
```

不采用：

```text
AgentPool
take()
run()
clearHistory()
offer()
```

也不推荐：

```text
每个 Step new 一个完整 SubAgent
```

而采用：

```text
共享 AgentDefinition
        +
每个 Step new AgentRun
```

---

# 2. 整体架构

```text
                           User Task
                               │
                               ▼
                           MasterRun
                               │
                               ▼
                            Planner
                               │
                               ▼
                           TaskGraph
                               │
                               ▼
                         StepScheduler
                               │
                      Agent 并发控制
                               │
              ┌────────────────┴────────────────┐
              ▼                                 ▼
         AgentRun-A                        AgentRun-B
              │                                 │
       Workspace-A                        Workspace-B
       / Worktree A                       / Worktree B
              │                                 │
              ▼                                 ▼
         AgentRuntime                       AgentRuntime
              │                                 │
              ▼                                 ▼
          ReActEngine                         ReActEngine
              │                                 │
              ▼                                 ▼
           LLM Call                          LLM Call
              │                                 │
         ToolCalls[]                       ToolCalls[]
              └────────────────┬────────────────┘
                               ▼
                    ToolExecutionManager
                               │
                       Tool 并发控制
                               │
              ┌────────────────┼────────────────┐
              ▼                ▼                ▼
          ToolRun-1        ToolRun-2        ToolRun-3
              │                │                │
             Tool             Tool             Tool
              │                │                │
              └────────────────┴────────────────┘
                               │
                               ▼
                          Observations
                               │
                               ▼
                     Local Verification
                               │
                               ▼
                          AgentResult
                               │
                               ▼
                     Global Verification
                               │
                    ┌──────────┴──────────┐
                    ▼                     ▼
                 Accept                 Replan
```

系统存在两层主要并行：

```text
第一层：AgentRun 并行
第二层：ToolRun 并行
```

---

# 3. 包结构建议

```text
com.xxx.codeagent
│
├── orchestration
│   ├── MasterRuntime.java
│   ├── Planner.java
│   ├── TaskGraph.java
│   ├── Step.java
│   ├── StepScheduler.java
│   └── GlobalVerifier.java
│
├── agent
│   ├── AgentDefinition.java
│   ├── AgentRegistry.java
│   ├── AgentRun.java
│   ├── RunContext.java
│   ├── AgentResult.java
│   └── RunStatus.java
│
├── runtime
│   ├── AgentRuntime.java
│   ├── ReActEngine.java
│   └── LocalVerifier.java
│
├── tool
│   ├── Tool.java
│   ├── ToolRegistry.java
│   ├── ToolCall.java
│   ├── ToolRun.java
│   ├── ToolResult.java
│   ├── ToolExecutionManager.java
│   ├── ToolConcurrencyMode.java
│   └── ResourceLockManager.java
│
├── workspace
│   ├── WorkspaceManager.java
│   ├── WorkspaceContext.java
│   └── GitWorktreeManager.java
│
├── infra
│   ├── ExecutionResources.java
│   ├── CancellationToken.java
│   ├── CancellationContext.java
│   ├── TraceManager.java
│   └── TimeoutManager.java
│
├── llm
│   ├── LLMClient.java
│   ├── LLMResponse.java
│   └── ModelConfig.java
│
└── mcp
    └── McpClient.java
```

---

# 4. AgentDefinition

`AgentDefinition` 只描述静态能力：

```java
public record AgentDefinition(
        String id,
        String name,
        String systemPrompt,
        ModelConfig modelConfig,
        List<String> allowedTools,
        int maxReActIterations,
        int maxReflectionCount
) {
    public AgentDefinition {
        allowedTools = List.copyOf(allowedTools);
    }
}
```

不要放：

```java
List<Message> history;
Step currentStep;
int retryCount;
ToolResult lastToolResult;
```

这些属于 `AgentRun / RunContext`。

---

# 5. AgentRun

```java
public final class AgentRun {

    private final String runId = UUID.randomUUID().toString();
    private final AgentDefinition definition;
    private final Step step;
    private final RunContext context;
    private final WorkspaceContext workspace;

    private RunStatus status = RunStatus.CREATED;
    private int reflectionCount;

    public AgentRun(
            AgentDefinition definition,
            Step step,
            WorkspaceContext workspace
    ) {
        this.definition = definition;
        this.step = step;
        this.workspace = workspace;
        this.context = new RunContext();
    }

    public String getRunId() { return runId; }
    public AgentDefinition getDefinition() { return definition; }
    public Step getStep() { return step; }
    public RunContext getContext() { return context; }
    public WorkspaceContext getWorkspace() { return workspace; }

    public int getReflectionCount() { return reflectionCount; }

    public void incrementReflectionCount() {
        reflectionCount++;
    }

    public void setStatus(RunStatus status) {
        this.status = status;
    }

    public RunStatus getStatus() {
        return status;
    }
}
```

---

# 6. RunContext

```java
public final class RunContext {

    private final List<Message> messages = new ArrayList<>();
    private final List<ToolRun> toolRuns = new ArrayList<>();

    private int reactIteration;
    private int retryCount;
    private String traceId;

    public void addMessage(Message message) {
        messages.add(message);
    }

    public void addToolRun(ToolRun toolRun) {
        toolRuns.add(toolRun);
    }

    public void incrementReActIteration() {
        reactIteration++;
    }

    public List<Message> getMessages() {
        return messages;
    }

    public List<ToolRun> getToolRuns() {
        return toolRuns;
    }

    public int getReActIteration() {
        return reactIteration;
    }
}
```

---

# 7. Workspace 隔离

Coding Agent 与普通 Agent 最大的区别之一，是多个 AgentRun 可能同时修改代码。

因此：

```text
AgentRun-A → Workspace-A
AgentRun-B → Workspace-B
```

推荐通过 Git Worktree 隔离：

```text
Repository
├── worktree/run-A
└── worktree/run-B
```

定义：

```java
public record WorkspaceContext(
        Path root,
        String branchName,
        String worktreeId
) {}
```

接口：

```java
public interface WorkspaceManager {

    WorkspaceContext create(String runId);

    void cleanup(WorkspaceContext workspace);
}
```

原则：

```text
读操作：
可以共享主仓库或独立 workspace

写操作：
优先独立 worktree

最终合并：
由 Master / Merge 阶段处理
```

---

# 8. AgentRuntime

`run()` 放在 `AgentRuntime`。

```java
public final class AgentRuntime {

    private final ReActEngine reActEngine;
    private final LocalVerifier localVerifier;
    private final WorkspaceManager workspaceManager;

    public AgentRuntime(
            ReActEngine reActEngine,
            LocalVerifier localVerifier,
            WorkspaceManager workspaceManager
    ) {
        this.reActEngine = reActEngine;
        this.localVerifier = localVerifier;
        this.workspaceManager = workspaceManager;
    }

    public AgentResult run(
            AgentDefinition definition,
            Step step
    ) {
        WorkspaceContext workspace =
                workspaceManager.create(step.id());

        AgentRun run =
                new AgentRun(
                        definition,
                        step,
                        workspace
                );

        try {
            initialize(run);
            run.setStatus(RunStatus.RUNNING);

            while (true) {

                AgentResult result =
                        reActEngine.execute(run);

                VerificationResult verification =
                        localVerifier.verify(run, result);

                if (verification.accepted()) {
                    run.setStatus(RunStatus.SUCCESS);
                    return result;
                }

                run.incrementReflectionCount();

                if (run.getReflectionCount()
                        >= definition.maxReflectionCount()) {
                    run.setStatus(RunStatus.FAILED);
                    return AgentResult.failed(
                            run.getRunId(),
                            "超过最大本地验证次数"
                    );
                }

                run.getContext().addMessage(
                        Message.user(
                                verification.feedback()
                        )
                );
            }

        } finally {
            workspaceManager.cleanup(workspace);
        }
    }

    private void initialize(AgentRun run) {

        run.getContext().addMessage(
                Message.system(
                        run.getDefinition().systemPrompt()
                )
        );

        run.getContext().addMessage(
                Message.user(
                        run.getStep().instruction()
                )
        );
    }
}
```

---

# 9. ReActEngine

```java
public final class ReActEngine {

    private final LLMClient llmClient;
    private final ToolRegistry toolRegistry;
    private final ToolExecutionManager toolExecutionManager;

    public AgentResult execute(AgentRun run) {

        while (run.getContext().getReActIteration()
                < run.getDefinition().maxReActIterations()) {

            CancellationContext.throwIfCancelled();

            run.getContext().incrementReActIteration();

            List<Tool> tools =
                    toolRegistry.getTools(
                            run.getDefinition().allowedTools()
                    );

            LLMResponse response =
                    llmClient.chat(
                            run.getDefinition().modelConfig(),
                            run.getContext().getMessages(),
                            tools
                    );

            if (!response.hasToolCalls()) {
                return AgentResult.success(
                        run.getRunId(),
                        response.content()
                );
            }

            List<ToolResult> results =
                    toolExecutionManager.executeBatch(
                            run,
                            response.toolCalls()
                    );

            for (ToolResult result : results) {
                run.getContext().addMessage(
                        Message.tool(result)
                );
            }
        }

        return AgentResult.failed(
                run.getRunId(),
                "超过最大 ReAct 次数"
        );
    }
}
```

---

# 10. Agent 并行：Java 21 推荐方案

推荐：

```text
VirtualThread
+
Semaphore
```

线程负责执行任务，Semaphore 负责限制业务并发。

```java
public final class StepScheduler
        implements AutoCloseable {

    private final ExecutorService executor =
            Executors.newVirtualThreadPerTaskExecutor();

    private final Semaphore agentSemaphore;

    private final AgentRegistry agentRegistry;
    private final AgentRuntime agentRuntime;

    public StepScheduler(
            int maxConcurrency,
            AgentRegistry agentRegistry,
            AgentRuntime agentRuntime
    ) {
        this.agentSemaphore =
                new Semaphore(maxConcurrency);

        this.agentRegistry = agentRegistry;
        this.agentRuntime = agentRuntime;
    }

    public CompletableFuture<AgentResult> submit(
            Step step
    ) {
        AgentDefinition definition =
                agentRegistry.get(step.agentId());

        return CompletableFuture.supplyAsync(() -> {

            boolean acquired = false;

            try {
                agentSemaphore.acquire();
                acquired = true;

                return agentRuntime.run(
                        definition,
                        step
                );

            } catch (InterruptedException e) {
                Thread.currentThread().interrupt();
                throw new RuntimeException(e);

            } finally {
                if (acquired) {
                    agentSemaphore.release();
                }
            }

        }, executor);
    }

    @Override
    public void close() {
        executor.close();
    }
}
```

例如：

```text
maxAgentConcurrency = 2

Step A ─┐
Step B ─┤
Step C ─┤→ Virtual Thread
Step D ─┤
Step E ─┘
          ↓
     Semaphore(2)
          ↓
同时最多两个 AgentRun
```

---

# 11. Java 17 兼容方案

如果还没有 Java 21：

```java
ExecutorService agentExecutor =
        Executors.newFixedThreadPool(2);
```

此时线程池大小直接作为 AgentRun 并发上限。

但 Agent 和 Tool 必须用不同线程池。

---

# 12. Tool

Tool 本身应该尽量：

```text
无状态
线程安全
可共享
```

```java
public interface Tool {

    String name();

    ToolConcurrencyMode concurrencyMode();

    ToolResult execute(
            ToolExecutionContext context,
            JsonNode arguments
    );
}
```

`ToolExecutionContext`：

```java
public record ToolExecutionContext(
        String agentRunId,
        WorkspaceContext workspace,
        CancellationToken cancellationToken
) {}
```

这样工具不需要从全局变量里寻找当前 workspace。

---

# 13. ToolRun

```java
public final class ToolRun {

    private final String toolRunId =
            UUID.randomUUID().toString();

    private final String agentRunId;
    private final ToolCall call;

    private ToolRunStatus status =
            ToolRunStatus.CREATED;

    private ToolResult result;
    private Throwable error;

    public ToolRun(
            String agentRunId,
            ToolCall call
    ) {
        this.agentRunId = agentRunId;
        this.call = call;
    }

    public void markRunning() {
        status = ToolRunStatus.RUNNING;
    }

    public void markSuccess(ToolResult result) {
        this.result = result;
        status = ToolRunStatus.SUCCESS;
    }

    public void markFailed(Throwable error) {
        this.error = error;
        status = ToolRunStatus.FAILED;
    }
}
```

---

# 14. Tool 并发策略

不是所有 ToolCall 都可以并行。

```java
public enum ToolConcurrencyMode {

    READ_ONLY,

    EXCLUSIVE_RESOURCE,

    SERIAL
}
```

典型分类：

```text
READ_ONLY
- read_file
- search_code
- list_files
- git_log

EXCLUSIVE_RESOURCE
- write_file
- delete_file
- move_file

SERIAL
- git_commit
- package/install
- 某些全项目 build 操作
```

---

# 15. ToolExecutionManager

Java 21 推荐同样使用虚拟线程 + Semaphore：

```java
public final class ToolExecutionManager
        implements AutoCloseable {

    private final ExecutorService executor =
            Executors.newVirtualThreadPerTaskExecutor();

    private final Semaphore toolSemaphore;

    private final ToolRegistry toolRegistry;
    private final ResourceLockManager lockManager;

    public ToolExecutionManager(
            int maxToolConcurrency,
            ToolRegistry toolRegistry,
            ResourceLockManager lockManager
    ) {
        this.toolSemaphore =
                new Semaphore(maxToolConcurrency);

        this.toolRegistry = toolRegistry;
        this.lockManager = lockManager;
    }

    public List<ToolResult> executeBatch(
            AgentRun agentRun,
            List<ToolCall> calls
    ) {

        List<CompletableFuture<ToolResult>> futures =
                calls.stream()
                        .map(call ->
                                CompletableFuture.supplyAsync(
                                        () -> executeOne(
                                                agentRun,
                                                call
                                        ),
                                        executor
                                )
                        )
                        .toList();

        return futures.stream()
                .map(CompletableFuture::join)
                .toList();
    }

    private ToolResult executeOne(
            AgentRun agentRun,
            ToolCall call
    ) {
        boolean acquired = false;

        ToolRun toolRun =
                new ToolRun(
                        agentRun.getRunId(),
                        call
                );

        agentRun.getContext()
                .addToolRun(toolRun);

        try {
            toolSemaphore.acquire();
            acquired = true;

            toolRun.markRunning();

            Tool tool =
                    toolRegistry.get(
                            call.getName()
                    );

            ToolExecutionContext context =
                    new ToolExecutionContext(
                            agentRun.getRunId(),
                            agentRun.getWorkspace(),
                            CancellationContext.current()
                    );

            ToolResult result =
                    lockManager.executeWithPolicy(
                            tool,
                            call,
                            () -> tool.execute(
                                    context,
                                    call.getArguments()
                            )
                    );

            toolRun.markSuccess(result);
            return result;

        } catch (InterruptedException e) {
            Thread.currentThread().interrupt();
            toolRun.markFailed(e);
            throw new RuntimeException(e);

        } catch (Exception e) {
            toolRun.markFailed(e);
            throw e;

        } finally {
            if (acquired) {
                toolSemaphore.release();
            }
        }
    }

    @Override
    public void close() {
        executor.close();
    }
}
```

---

# 16. 为什么 Agent Executor 和 Tool Executor 必须分离

禁止：

```text
同一个 FixedThreadPool
同时跑 AgentRun 和 ToolRun
```

否则可能出现：

```text
Thread-1 → Agent A 等工具
Thread-2 → Agent B 等工具

Tool A / Tool B
又排在同一个线程池里

结果：
Agent 等 Tool
Tool 等线程
```

Java 21 虚拟线程降低了这个风险，但业务并发仍应分别限流：

```text
agentSemaphore
toolSemaphore
```

---

# 17. TaskGraph

Planner 应逐步从：

```java
List<Step>
```

升级为：

```text
TaskGraph
```

例如：

```text
      A
    /   \
   B     C
    \   /
      D
```

定义：

```java
public record Step(
        String id,
        String agentId,
        String instruction,
        Set<String> dependencies
) {}
```

调度规则：

```text
只有 dependencies 全部完成的 Step
才能进入 Ready Queue
```

---

# 18. Local Reflection / Verification

属于单个 AgentRun：

```text
ReAct
 ↓
执行
 ↓
测试
 ↓
Verifier
 ↓
通过？── Yes → Result
 │
 No
 ↓
反馈写回 RunContext
 ↓
继续 ReAct
```

建议接口：

```java
public interface LocalVerifier {

    VerificationResult verify(
            AgentRun run,
            AgentResult result
    );
}
```

---

# 19. Global Verification

属于 Master：

```text
多个 AgentResult
      ↓
GlobalVerifier
      ↓
是否完成用户目标？
      │
      ├── Yes → Finish
      └── No  → Replan
```

避免把 Reflection 固定成“每一步必经阶段”，更适合把它看作：

```text
验证策略
+
修正策略
```

---

# 20. MasterRuntime

```java
public final class MasterRuntime {

    private final Planner planner;
    private final StepScheduler scheduler;
    private final GlobalVerifier verifier;

    public FinalResult run(UserTask task) {

        TaskGraph graph =
                planner.plan(task);

        while (true) {

            Map<String, AgentResult> results =
                    scheduler.execute(graph);

            GlobalVerificationResult verification =
                    verifier.verify(
                            task,
                            graph,
                            results
                    );

            if (verification.accepted()) {
                return FinalResult.of(results);
            }

            graph =
                    planner.replan(
                            task,
                            graph,
                            results,
                            verification
                    );
        }
    }
}
```

---

# 21. Cancellation

取消链：

```text
MasterRun Cancel
      ↓
AgentRun Cancel
      ↓
ReAct Stop
      ↓
ToolRun Cancel
```

主要循环都检查：

```java
CancellationContext.throwIfCancelled();
```

Tool 也接收：

```java
CancellationToken
```

避免 Tool 自己访问不透明全局状态。

---

# 22. Timeout

建议三层：

```text
MasterRun Timeout
AgentRun Timeout
ToolRun Timeout
```

例如：

```yaml
master:
  timeout: 10m

agent:
  timeout: 5m

tool:
  timeout: 60s
```

---

# 23. Trace

建议：

```text
masterRunId
  └── agentRunId
       ├── llmCallId
       ├── toolRunId
       └── verificationId
```

日志核心 ID 不应再是：

```text
worker-1
worker-2
```

而是：

```text
masterRunId
agentRunId
stepId
toolRunId
workspaceId
```

---

# 24. 推荐配置

```yaml
agent:
  max-concurrency: 2
  max-react-iterations: 10
  max-reflection-count: 3

tool:
  max-concurrency: 8

workspace:
  isolation: worktree

timeout:
  agent-run-seconds: 300
  tool-run-seconds: 60
```

---

# 25. 实现阶段

## 第一阶段

实现：

```text
AgentDefinition
AgentRun
RunContext
AgentRuntime
ReActEngine
StepScheduler
ToolExecutionManager
```

先做到：

```text
Agent 并发
+
Tool 并发
+
状态隔离
```

---

## 第二阶段

增加：

```text
TaskGraph
LocalVerifier
GlobalVerifier
Cancellation
Timeout
Trace
```

---

## 第三阶段

增加：

```text
Git Worktree Workspace
ResourceLock
动态 Agent 选择
Tool Dependency
Run 持久化
分布式调度
```

---

# 26. 最终原则

```text
共享：
AgentDefinition
AgentRuntime
Tool
LLMClient

独立：
AgentRun
RunContext
ToolRun
Workspace

调度：
StepScheduler 控 AgentRun
ToolExecutionManager 控 ToolRun

隔离：
Workspace / Worktree

修正：
Local Verification
Global Verification
```

一句话总结：

> **不要复用运行状态，只复用能力定义和基础设施；每个任务创建独立 AgentRun，每次工具调用创建独立 ToolRun，并让 Agent 调度、Tool 调度、Workspace 隔离三个层次彼此独立。**
