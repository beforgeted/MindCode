# CodeAgent Multi-Agent 并行架构设计方案（Python 版）

> 目标：实现一套支持 **Plan + Multi-Agent 并行 + ReAct + Tool 并行 + Reflection / Verification + Workspace 隔离** 的 Coding Agent 架构。  
> 推荐基础：**Python 3.11+ + asyncio**。I/O 型 LLM / MCP / 文件 / 网络操作优先异步化。

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
Tool            = 工具能力
ToolRun         = 某次工具调用
Workspace       = 某次 AgentRun 的代码工作区
```

不采用：

```text
AgentPool
borrow()
run()
clear_history()
return()
```

也不推荐：

```text
每个 Step new 一个完整 SubAgent
```

而采用：

```text
共享 AgentDefinition
        +
每个 Step 创建新的 AgentRun
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

两层主要并行：

```text
第一层：AgentRun 并行
第二层：ToolRun 并行
```

---

# 3. 目录结构建议

```text
codeagent/
│
├── orchestration/
│   ├── master_runtime.py
│   ├── planner.py
│   ├── task_graph.py
│   ├── step.py
│   ├── step_scheduler.py
│   └── global_verifier.py
│
├── agent/
│   ├── definition.py
│   ├── registry.py
│   ├── run.py
│   ├── context.py
│   ├── result.py
│   └── status.py
│
├── runtime/
│   ├── agent_runtime.py
│   ├── react_engine.py
│   └── local_verifier.py
│
├── tool/
│   ├── base.py
│   ├── registry.py
│   ├── call.py
│   ├── run.py
│   ├── result.py
│   ├── execution_manager.py
│   ├── concurrency.py
│   └── resource_lock.py
│
├── workspace/
│   ├── manager.py
│   ├── context.py
│   └── git_worktree.py
│
├── infra/
│   ├── cancellation.py
│   ├── tracing.py
│   └── timeout.py
│
├── llm/
│   ├── client.py
│   ├── response.py
│   └── config.py
│
└── mcp/
    └── client.py
```

---

# 4. AgentDefinition

推荐不可变 dataclass：

```python
from dataclasses import dataclass
from typing import Tuple

@dataclass(frozen=True)
class AgentDefinition:
    id: str
    name: str
    system_prompt: str
    model_config: "ModelConfig"
    allowed_tools: Tuple[str, ...]
    max_react_iterations: int = 10
    max_reflection_count: int = 3
```

不要放：

```python
history
current_step
retry_count
last_tool_result
```

这些属于 `AgentRun / RunContext`。

---

# 5. RunContext

```python
from dataclasses import dataclass, field

@dataclass
class RunContext:
    messages: list["Message"] = field(default_factory=list)
    tool_runs: list["ToolRun"] = field(default_factory=list)

    react_iteration: int = 0
    retry_count: int = 0

    trace_id: str | None = None
```

---

# 6. WorkspaceContext

```python
from dataclasses import dataclass
from pathlib import Path

@dataclass(frozen=True)
class WorkspaceContext:
    root: Path
    worktree_id: str
    branch_name: str
```

接口：

```python
from typing import Protocol

class WorkspaceManager(Protocol):

    async def create(
        self,
        run_id: str,
    ) -> WorkspaceContext:
        ...

    async def cleanup(
        self,
        workspace: WorkspaceContext,
    ) -> None:
        ...
```

推荐：

```text
AgentRun-A → Worktree A
AgentRun-B → Worktree B
```

避免两个并行 Agent 直接写同一个代码目录。

---

# 7. AgentRun

```python
from dataclasses import dataclass, field
from uuid import uuid4

@dataclass
class AgentRun:
    definition: AgentDefinition
    step: "Step"
    workspace: WorkspaceContext

    run_id: str = field(
        default_factory=lambda: str(uuid4())
    )

    context: RunContext = field(
        default_factory=RunContext
    )

    status: str = "created"
    reflection_count: int = 0
```

生命周期：

```text
Step
 ↓
create AgentRun
 ↓
执行
 ↓
完成
 ↓
释放 / 持久化
```

---

# 8. AgentRuntime

`run()` 放在 `AgentRuntime`。

```python
class AgentRuntime:

    def __init__(
        self,
        react_engine: "ReActEngine",
        local_verifier: "LocalVerifier",
        workspace_manager: "WorkspaceManager",
    ):
        self._react_engine = react_engine
        self._local_verifier = local_verifier
        self._workspace_manager = workspace_manager

    async def run(
        self,
        definition: AgentDefinition,
        step: "Step",
    ) -> "AgentResult":

        workspace = await self._workspace_manager.create(
            step.id
        )

        run = AgentRun(
            definition=definition,
            step=step,
            workspace=workspace,
        )

        try:
            self._initialize(run)
            run.status = "running"

            while True:

                result = await self._react_engine.execute(
                    run
                )

                verification = (
                    await self._local_verifier.verify(
                        run,
                        result,
                    )
                )

                if verification.accepted:
                    run.status = "success"
                    return result

                run.reflection_count += 1

                if (
                    run.reflection_count
                    >= definition.max_reflection_count
                ):
                    run.status = "failed"

                    return AgentResult.failed(
                        run.run_id,
                        "超过最大本地验证次数",
                    )

                run.context.messages.append(
                    Message.user(
                        verification.feedback
                    )
                )

        finally:
            await self._workspace_manager.cleanup(
                workspace
            )

    def _initialize(
        self,
        run: AgentRun,
    ) -> None:

        run.context.messages.append(
            Message.system(
                run.definition.system_prompt
            )
        )

        run.context.messages.append(
            Message.user(
                run.step.instruction
            )
        )
```

---

# 9. ReActEngine

```python
class ReActEngine:

    def __init__(
        self,
        llm_client: "LLMClient",
        tool_registry: "ToolRegistry",
        tool_execution_manager: "ToolExecutionManager",
    ):
        self._llm_client = llm_client
        self._tool_registry = tool_registry
        self._tool_execution_manager = (
            tool_execution_manager
        )

    async def execute(
        self,
        run: AgentRun,
    ) -> "AgentResult":

        while (
            run.context.react_iteration
            < run.definition.max_react_iterations
        ):

            check_cancelled()

            run.context.react_iteration += 1

            tools = self._tool_registry.get_tools(
                run.definition.allowed_tools
            )

            response = await self._llm_client.chat(
                model_config=run.definition.model_config,
                messages=run.context.messages,
                tools=tools,
            )

            if not response.tool_calls:

                return AgentResult.success(
                    run.run_id,
                    response.content,
                )

            results = (
                await self
                ._tool_execution_manager
                .execute_batch(
                    run,
                    response.tool_calls,
                )
            )

            for result in results:
                run.context.messages.append(
                    Message.tool(result)
                )

        return AgentResult.failed(
            run.run_id,
            "超过最大 ReAct 次数",
        )
```

---

# 10. Agent 并行：asyncio.Semaphore

Python 不需要为 AgentRun 创建对象池。

核心：

```text
asyncio task
+
Semaphore
```

```python
import asyncio

class StepScheduler:

    def __init__(
        self,
        max_concurrency: int,
        agent_registry: "AgentRegistry",
        agent_runtime: AgentRuntime,
    ):
        self._agent_semaphore = asyncio.Semaphore(
            max_concurrency
        )

        self._agent_registry = agent_registry
        self._agent_runtime = agent_runtime

    async def run_step(
        self,
        step: "Step",
    ) -> "AgentResult":

        definition = self._agent_registry.get(
            step.agent_id
        )

        async with self._agent_semaphore:

            return await self._agent_runtime.run(
                definition,
                step,
            )
```

批量执行：

```python
async def execute_ready_steps(
    self,
    steps: list["Step"],
) -> list["AgentResult"]:

    async with asyncio.TaskGroup() as tg:

        tasks = [
            tg.create_task(
                self.run_step(step)
            )
            for step in steps
        ]

    return [
        task.result()
        for task in tasks
    ]
```

效果：

```text
Step A ─┐
Step B ─┤
Step C ─┤→ asyncio Tasks
Step D ─┤
Step E ─┘
          ↓
   Semaphore(2)
          ↓
同时最多两个 AgentRun
```

---

# 11. Tool 接口

Tool 本身尽量：

```text
无状态
线程安全 / 协程安全
长期共享
```

```python
from typing import Protocol

class Tool(Protocol):

    @property
    def name(self) -> str:
        ...

    @property
    def concurrency_mode(
        self,
    ) -> "ToolConcurrencyMode":
        ...

    async def execute(
        self,
        context: "ToolExecutionContext",
        arguments: dict,
    ) -> "ToolResult":
        ...
```

如果 Tool 是同步阻塞函数：

```python
result = await asyncio.to_thread(
    blocking_tool.execute,
    context,
    arguments,
)
```

不要直接在 event loop 中执行长时间阻塞 I/O。

---

# 12. ToolExecutionContext

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class ToolExecutionContext:
    agent_run_id: str
    workspace: WorkspaceContext
    cancellation_token: "CancellationToken"
```

工具只通过显式 Context 获取：

```text
当前 AgentRun
当前 Workspace
当前 CancellationToken
```

避免依赖隐式全局状态。

---

# 13. ToolRun

```python
from dataclasses import dataclass, field
from uuid import uuid4

@dataclass
class ToolRun:
    agent_run_id: str
    call: "ToolCall"

    tool_run_id: str = field(
        default_factory=lambda: str(uuid4())
    )

    status: str = "created"
    result: "ToolResult | None" = None
    error: Exception | None = None
```

每次 ToolCall：

```text
create ToolRun
 ↓
execute
 ↓
save result
```

---

# 14. ToolConcurrencyMode

```python
from enum import Enum

class ToolConcurrencyMode(str, Enum):
    READ_ONLY = "read_only"
    EXCLUSIVE_RESOURCE = "exclusive_resource"
    SERIAL = "serial"
```

分类示例：

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
- package install
- 某些全项目 build
```

---

# 15. Tool 并行：asyncio.Semaphore

```python
class ToolExecutionManager:

    def __init__(
        self,
        max_concurrency: int,
        tool_registry: "ToolRegistry",
        resource_lock_manager: "ResourceLockManager",
    ):
        self._tool_semaphore = asyncio.Semaphore(
            max_concurrency
        )

        self._tool_registry = tool_registry

        self._resource_lock_manager = (
            resource_lock_manager
        )
```

---

# 16. Tool Batch 并行执行

```python
async def execute_batch(
    self,
    agent_run: AgentRun,
    calls: list["ToolCall"],
) -> list["ToolResult"]:

    async with asyncio.TaskGroup() as tg:

        tasks = [
            tg.create_task(
                self._execute_one(
                    agent_run,
                    call,
                )
            )
            for call in calls
        ]

    return [
        task.result()
        for task in tasks
    ]
```

单个 Tool：

```python
async def _execute_one(
    self,
    agent_run: AgentRun,
    call: "ToolCall",
) -> "ToolResult":

    tool_run = ToolRun(
        agent_run_id=agent_run.run_id,
        call=call,
    )

    agent_run.context.tool_runs.append(
        tool_run
    )

    async with self._tool_semaphore:

        tool_run.status = "running"

        try:
            tool = self._tool_registry.get(
                call.name
            )

            context = ToolExecutionContext(
                agent_run_id=agent_run.run_id,
                workspace=agent_run.workspace,
                cancellation_token=current_token(),
            )

            result = await (
                self._resource_lock_manager
                .execute_with_policy(
                    tool,
                    call,
                    lambda: tool.execute(
                        context,
                        call.arguments,
                    ),
                )
            )

            tool_run.status = "success"
            tool_run.result = result

            return result

        except Exception as exc:

            tool_run.status = "failed"
            tool_run.error = exc

            raise
```

---

# 17. 为什么 Python 更适合 asyncio

Coding Agent 主要耗时通常来自：

```text
LLM HTTP
MCP
网络
文件
数据库
Shell Process 等待
```

这些大部分是 I/O 等待。

因此推荐：

```text
asyncio.Task
+
asyncio.Semaphore
```

而不是：

```text
ThreadPoolExecutor 控所有东西
```

但阻塞库仍可通过：

```python
asyncio.to_thread(...)
```

接入。

---

# 18. Agent 并发与 Tool 并发分开限流

```python
agent_semaphore = asyncio.Semaphore(2)

tool_semaphore = asyncio.Semaphore(8)
```

结构：

```text
                    StepScheduler
                         │
                  Agent Semaphore(2)
                         │
           ┌─────────────┴─────────────┐
           ▼                           ▼
      AgentRun-A                  AgentRun-B
           │                           │
         ReAct                       ReAct
           │                           │
        ToolCall                    ToolCall
           └─────────────┬─────────────┘
                         ▼
              ToolExecutionManager
                         │
                   Tool Semaphore(8)
                         │
      ┌───────┬──────────┼──────────┬───────┐
      ▼       ▼          ▼          ▼       ▼
     T1      T2         T3         T4      T5
```

---

# 19. Tool 并行不能无脑执行

例如：

```text
read_file(A.py)
read_file(B.py)
search_code("foo")
```

可以并行。

但：

```text
write_file(A.py)
pytest
```

存在：

```text
write
 ↓
test
```

不能直接并行。

更进一步：

```text
Agent A 写 src/user.py
Agent B 写 src/user.py
```

也需要资源锁或 workspace 隔离。

---

# 20. ResourceLockManager

可以用：

```python
dict[str, asyncio.Lock]
```

按资源 key 锁：

```text
file:/repo/src/user.py
workspace:/repo/worktree-A
git-index:/repo/worktree-A
build:/repo/worktree-A
```

示意：

```python
class ResourceLockManager:

    def __init__(self):
        self._locks: dict[str, asyncio.Lock] = {}

    def _get_lock(
        self,
        key: str,
    ) -> asyncio.Lock:

        return self._locks.setdefault(
            key,
            asyncio.Lock(),
        )
```

真正实现时需要考虑锁表清理和并发访问。

---

# 21. TaskGraph

Planner 不建议长期只输出：

```python
list[Step]
```

应升级为 DAG：

```text
      A
    /   \
   B     C
    \   /
      D
```

```python
from dataclasses import dataclass, field

@dataclass(frozen=True)
class Step:
    id: str
    agent_id: str
    instruction: str
    dependencies: frozenset[str] = field(
        default_factory=frozenset
    )
```

调度：

```text
dependencies 全部完成
        ↓
Step 进入 Ready Set
        ↓
asyncio Task
```

---

# 22. Local Verification

属于单个 AgentRun：

```text
ReAct
 ↓
Result
 ↓
LocalVerifier
 ↓
通过？
├── Yes → Return
└── No  → feedback → ReAct
```

```python
class LocalVerifier(Protocol):

    async def verify(
        self,
        run: AgentRun,
        result: "AgentResult",
    ) -> "VerificationResult":
        ...
```

---

# 23. Global Verification

Master 收集结果：

```text
AgentResult A
AgentResult B
AgentResult C
      ↓
GlobalVerifier
      ↓
Accept / Replan
```

不要把 Reflection 设计成所有流程都必须经过的固定状态机。

更灵活的理解：

```text
Reflection = 反馈
Verification = 判断目标是否满足
Replan = 重新规划
```

---

# 24. MasterRuntime

```python
class MasterRuntime:

    def __init__(
        self,
        planner: "Planner",
        scheduler: StepScheduler,
        global_verifier: "GlobalVerifier",
    ):
        self._planner = planner
        self._scheduler = scheduler
        self._global_verifier = global_verifier

    async def run(
        self,
        task: "UserTask",
    ) -> "FinalResult":

        graph = await self._planner.plan(task)

        while True:

            results = await (
                self._scheduler.execute(graph)
            )

            verification = (
                await self._global_verifier.verify(
                    task,
                    graph,
                    results,
                )
            )

            if verification.accepted:
                return FinalResult.of(results)

            graph = await self._planner.replan(
                task,
                graph,
                results,
                verification,
            )
```

---

# 25. Cancellation

Python 推荐显式 Token + asyncio Task Cancel 结合。

```python
class CancellationToken:

    def __init__(self):
        self._event = asyncio.Event()

    def cancel(self):
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()
```

取消链：

```text
MasterRun cancel
      ↓
cancel Agent tasks
      ↓
ReAct stop
      ↓
cancel Tool tasks
```

同时要正确传播：

```python
asyncio.CancelledError
```

不要吞掉。

---

# 26. Timeout

推荐：

```python
async with asyncio.timeout(300):
    result = await agent_runtime.run(...)
```

Tool：

```python
async with asyncio.timeout(60):
    result = await tool.execute(...)
```

分别设置：

```text
MasterRun Timeout
AgentRun Timeout
ToolRun Timeout
```

---

# 27. Trace

建议 ID：

```text
master_run_id
  └── agent_run_id
       ├── llm_call_id
       ├── tool_run_id
       └── verification_id
```

不要依赖：

```text
worker-1
worker-2
```

作为业务追踪标识。

---

# 28. 推荐配置

```yaml
agent:
  max_concurrency: 2
  max_react_iterations: 10
  max_reflection_count: 3

tool:
  max_concurrency: 8

workspace:
  isolation: worktree

timeout:
  agent_run_seconds: 300
  tool_run_seconds: 60
```

---

# 29. 第一阶段实现

先实现：

```text
AgentDefinition
AgentRun
RunContext
AgentRuntime
ReActEngine
StepScheduler
ToolExecutionManager
```

做到：

```text
Agent 并发
+
Tool 并发
+
状态隔离
```

---

# 30. 第二阶段

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

# 31. 第三阶段

增加：

```text
Git Worktree
ResourceLock
Tool Dependency
动态 Agent
Run 持久化
分布式 Worker
```

---

# 32. Python 版最终原则

```text
共享：
AgentDefinition
AgentRuntime
Tool
ToolRegistry
LLMClient

独立：
AgentRun
RunContext
ToolRun
Workspace

并发：
asyncio.Task
+
asyncio.Semaphore

Agent：
agent_semaphore

Tool：
tool_semaphore

隔离：
Workspace / Git Worktree

修正：
Local Verification
Global Verification
Replan
```

一句话总结：

> **Python 版不需要 Agent 对象池；用 asyncio Task 表示执行，用 Semaphore 控制并发，每个 Step 创建独立 AgentRun，每次 ToolCall 创建独立 ToolRun，并通过独立 Workspace 隔离代码修改。**
