"""ToolExecutionManager。

这里修掉了两份 Multi-Agent 文档里同一个真实 bug。

文档的 `_execute_one` 末尾是 `raise`，外层用 `asyncio.TaskGroup`（Java 版是
`CompletableFuture::join`）。TaskGroup 在第一个异常时会取消所有兄弟任务，
于是任一 tool 失败 = 整批被取消 = assistant 的 N 个 tool_call 只回来不到 N 个
tool_result —— 正是上下文文档 §37.1 明令禁止的孤立 tool_call。

本实现的不变式：

    len(results) == len(calls)，无条件成立。

做法有三层：
1. `_execute_one` 永不抛（异常一律转 ToolResult.error）；
2. `gather(..., return_exceptions=True)` 再兜一道；
3. 最后从 ToolRun 记录里补洞，任何缺失的 call 都补一条结果。

注意捕获范围只能是 `except Exception`。写成 `except BaseException` 或裸 `except`
会吞掉 CancelledError（3.8+ 起它继承自 BaseException），取消就失效了。
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from dataclasses import dataclass

from codeagent.context.profile import ContextProfile
from codeagent.evidence.artifact_store import ArtifactStore
from codeagent.evidence.event_store import NullEventStore, RawEventStore
from codeagent.evidence.models import AgentEvent, EventType, EvidenceRef, EvidenceType
from codeagent.infra import metrics as M
from codeagent.infra.cancellation import CancellationToken, CancelledByUser
from codeagent.infra.metrics import Metrics
from codeagent.tool.base import Tool, ToolExecutionContext
from codeagent.tool.models import (
    ToolCall,
    ToolConcurrencyMode,
    ToolResult,
    ToolResultStatus,
    ToolRun,
)
from codeagent.tool.normalizer import ToolResultNormalizer
from codeagent.tool.registry import ToolNotFoundError, ToolRegistry
from codeagent.tool.resource_lock import ResourceLockManager
from codeagent.workspace.context import WorkspaceContext


@dataclass(frozen=True, slots=True)
class ExecutionScope:
    """一次 tool batch 需要的运行期上下文。

    刻意不传 AgentRun：tool 层不应该依赖 agent 层（否则循环依赖），
    工具需要的东西一律显式传入。
    """

    agent_run_id: str
    session_id: str
    workspace: WorkspaceContext
    cancellation: CancellationToken
    profile: ContextProfile
    turn_id: str | None = None


@dataclass(frozen=True, slots=True)
class ToolBatchOutcome:
    results: tuple[ToolResult, ...]
    tool_runs: tuple[ToolRun, ...] = ()
    cancelled: bool = False
    ran_serially: bool = False


class ToolExecutionManager:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        normalizer: ToolResultNormalizer,
        artifact_store: ArtifactStore,
        max_concurrency: int = 8,
        lock_manager: ResourceLockManager | None = None,
        event_store: RawEventStore | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self._registry = registry
        self._normalizer = normalizer
        self._artifacts = artifact_store
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._locks = lock_manager or ResourceLockManager()
        self._events: RawEventStore = event_store or NullEventStore()
        self._metrics = metrics or Metrics()

    async def execute_batch(
        self, scope: ExecutionScope, calls: Sequence[ToolCall]
    ) -> ToolBatchOutcome:
        if not calls:
            return ToolBatchOutcome(())

        runs = [ToolRun(agent_run_id=scope.agent_run_id, call=call) for call in calls]
        serial = any(self._mode_of(call.name) is ToolConcurrencyMode.SERIAL for call in calls)

        if serial:
            # SERIAL 工具（git_commit / 装包 / 全项目 build）不能与任何东西并行，
            # 整批退化为 LLM 给出的顺序执行。比引入读写锁简单且显然正确。
            for run in runs:
                await self._execute_one(scope, run)
        else:
            await asyncio.gather(
                *(self._execute_one(scope, run) for run in runs),
                return_exceptions=True,
            )

        results: list[ToolResult] = []
        cancelled = False
        for run in runs:
            result = run.result
            if result is None:
                # 补洞：无论上面发生了什么，每个 call 都必须有结果。
                result = ToolResult.cancelled(run.call)
                run.finish(result)
            if result.status is ToolResultStatus.CANCELLED:
                cancelled = True
            results.append(result)

        return ToolBatchOutcome(
            results=tuple(results),
            tool_runs=tuple(runs),
            cancelled=cancelled,
            ran_serially=serial,
        )

    def _mode_of(self, name: str) -> ToolConcurrencyMode:
        try:
            return self._registry.get(name).concurrency_mode
        except ToolNotFoundError:
            return ToolConcurrencyMode.READ_ONLY

    async def _execute_one(self, scope: ExecutionScope, run: ToolRun) -> None:
        call = run.call
        self._metrics.incr(M.TOOL_RUNS)
        self._events.append_nowait(
            AgentEvent(
                type=EventType.TOOL_CALL,
                session_id=scope.session_id,
                agent_run_id=scope.agent_run_id,
                tool_run_id=run.tool_run_id,
                turn_id=scope.turn_id,
                payload={"name": call.name, "arguments": call.arguments},
            )
        )

        result = await self._run_guarded(scope, run)

        try:
            result = await self._normalizer.normalize(result, profile=scope.profile)
        except Exception as exc:  # normalizer 自身失败不应让整个 call 失败
            result = ToolResult(
                call_id=call.id,
                tool_name=call.name,
                status=result.status,
                content=f"{result.content[:2000]}\n[归一化失败: {exc}]",
                exit_code=result.exit_code,
                truncated=True,
            )

        result = ToolResult(
            # 强制回填协议 id：不变式不能依赖每个工具实现都写对。
            call_id=call.id,
            tool_name=call.name,
            status=result.status,
            content=result.content,
            exit_code=result.exit_code,
            artifact=result.artifact,
            evidence=EvidenceRef(
                type=EvidenceType.TOOL_RESULT,
                session_id=scope.session_id,
                agent_run_id=scope.agent_run_id,
                tool_run_id=run.tool_run_id,
                artifact_uri=result.artifact_uri,
            ),
            truncated=result.truncated,
            raw_bytes=result.raw_bytes,
            metadata=result.metadata,
        )

        run.finish(result)
        if result.status is ToolResultStatus.ERROR:
            self._metrics.incr(M.TOOL_ERRORS)
        elif result.status is ToolResultStatus.TIMEOUT:
            self._metrics.incr(M.TOOL_TIMEOUTS)
        elif result.status is ToolResultStatus.CANCELLED:
            self._metrics.incr(M.TOOL_CANCELLED)
        if run.duration_ms is not None:
            self._metrics.observe(M.TOOL_EXEC_MS, run.duration_ms)

        self._events.append_nowait(
            AgentEvent(
                type=EventType.TOOL_RESULT,
                session_id=scope.session_id,
                agent_run_id=scope.agent_run_id,
                tool_run_id=run.tool_run_id,
                turn_id=scope.turn_id,
                payload={
                    "name": call.name,
                    "status": str(result.status),
                    "exit_code": result.exit_code,
                    "artifact_uri": result.artifact_uri,
                    "truncated": result.truncated,
                    "content_preview": result.content[:2000],
                },
            )
        )

    async def _run_guarded(self, scope: ExecutionScope, run: ToolRun) -> ToolResult:
        call = run.call
        try:
            tool = self._registry.get(call.name)
        except ToolNotFoundError:
            return ToolResult.error(
                call, f"未知工具 '{call.name}'。可用工具: {', '.join(self._registry.names())}"
            )

        if scope.cancellation.cancelled:
            return ToolResult.cancelled(call)

        ctx = ToolExecutionContext(
            agent_run_id=scope.agent_run_id,
            session_id=scope.session_id,
            tool_run_id=run.tool_run_id,
            call_id=call.id,
            workspace=scope.workspace,
            cancellation=scope.cancellation,
            artifact_store=self._artifacts,
            max_output_bytes=scope.profile.max_tool_output_bytes,
            timeout_seconds=scope.profile.tool_timeout_seconds,
        )
        keys = _resource_keys(tool, call)

        run.mark_running()
        try:
            # timeout 放最外层：它要同时约束"等信号量/等资源锁"和"真正执行"，
            # 否则一个拿不到锁的调用会永远挂住。
            async with asyncio.timeout(scope.profile.tool_timeout_seconds):
                async with self._semaphore:
                    async with self._locks.acquire(keys):
                        return await tool.execute(ctx, call.arguments)
        except CancelledByUser:
            return ToolResult.cancelled(call)
        except TimeoutError:
            return ToolResult.timeout(call, scope.profile.tool_timeout_seconds)
        except Exception as exc:  # 绝不能是 BaseException / 裸 except
            run.error = f"{type(exc).__name__}: {exc}"
            return ToolResult.error(call, f"工具执行失败: {type(exc).__name__}: {exc}")


def _resource_keys(tool: Tool, call: ToolCall) -> tuple[str, ...]:
    if tool.concurrency_mode is ToolConcurrencyMode.READ_ONLY:
        return ()
    try:
        return tuple(tool.resource_keys(call.arguments))
    except Exception:
        return ()
