"""ReActEngine：Agent 主循环。

它只负责 Agent Loop，完全不知道任何上下文策略 —— 裁剪、预算、压缩全部在
`context_manager.prepare()` 里。这是"Agent 负责 Agent Loop，ContextManager
负责 Context Engineering"的落地。

tool 协议约束：一个 assistant turn 的**全部** tool_result 必须放进紧随其后的
同一条消息里。ToolExecutionManager 保证 `len(results) == len(calls)`，
这里保证它们成为一条完整的 tool 消息，两者合起来使
`validate_tool_protocol()` 恒成立。
"""

from __future__ import annotations

from collections.abc import Sequence

from codeagent.agent.models import (
    AgentRunResult,
    FileChangeKind,
    FileState,
    RunStatus,
    TestOutcome,
    TestState,
)
from codeagent.agent.run import AgentRun
from codeagent.context.manager import ContextManager
from codeagent.evidence.event_store import NullEventStore, RawEventStore
from codeagent.evidence.models import AgentEvent, EventType
from codeagent.infra import metrics as M
from codeagent.infra.metrics import Metrics
from codeagent.infra.text import extract_test_counts
from codeagent.llm.client import LlmClient
from codeagent.llm.message import Message, TextBlock, ToolResultBlock
from codeagent.tool.execution_manager import ExecutionScope, ToolExecutionManager
from codeagent.tool.models import ToolCall, ToolResult, ToolResultStatus, ToolRun
from codeagent.tool.registry import ToolRegistry


class ReActEngine:
    def __init__(
        self,
        *,
        llm_client: LlmClient,
        registry: ToolRegistry,
        execution_manager: ToolExecutionManager,
        context_manager: ContextManager,
        event_store: RawEventStore | None = None,
        metrics: Metrics | None = None,
    ) -> None:
        self._llm = llm_client
        self._registry = registry
        self._tools = execution_manager
        self._context = context_manager
        self._events: RawEventStore = event_store or NullEventStore()
        self._metrics = metrics or Metrics()

    async def run_turn(self, run: AgentRun, user_input: str) -> AgentRunResult:
        run.status = RunStatus.RUNNING
        self._events.append_nowait(
            AgentEvent(
                type=EventType.AGENT_RUN_STARTED,
                session_id=run.session_id,
                agent_run_id=run.run_id,
                payload={"agent": run.definition.id, "input": user_input[:2000]},
            )
        )

        turn_id = run.history.begin_turn()
        run.history.append(Message.user(user_input, turn_id=turn_id))
        specs = self._registry.specs(run.definition.allowed_tools or None)
        collected: list[ToolRun] = []

        while run.context.react_iteration < run.definition.max_react_iterations:
            if run.cancellation.cancelled:
                return self._finish(run, RunStatus.CANCELLED, "用户取消", collected)

            run.context.react_iteration += 1
            self._metrics.incr(M.REACT_ITERATIONS)

            mprofile = run.definition.memory_profile
            prepared = await self._context.prepare(
                run.history,
                run.profile,
                memory_type_filter=mprofile.readable_types or None,
                memory_injection_cap=mprofile.max_injection_tokens,
            )
            run.context.last_prepared = prepared
            response = await self._llm.chat(
                prepared.messages,
                model_config=run.definition.model_config,
                tools=specs,
            )

            blocks = response.blocks or (TextBlock(response.content),)
            run.history.append(Message.assistant(blocks, turn_id=turn_id))

            if not response.has_tool_uses:
                return self._finish(run, RunStatus.SUCCESS, response.content, collected)

            calls = [
                ToolCall(id=block.id, name=block.name, arguments=block.arguments)
                for block in response.tool_uses
            ]
            scope = ExecutionScope(
                agent_run_id=run.run_id,
                session_id=run.session_id,
                workspace=run.workspace,
                cancellation=run.cancellation,
                profile=run.profile,
                turn_id=turn_id,
            )
            outcome = await self._tools.execute_batch(scope, calls)
            run.context.record_tool_runs(outcome.tool_runs)
            collected.extend(outcome.tool_runs)

            # 协议完整性优先：先把结果写回历史，再处理取消。
            run.history.append(
                Message.tool(
                    [_to_block(result) for result in outcome.results],
                    turn_id=turn_id,
                )
            )

            if outcome.cancelled:
                return self._finish(run, RunStatus.CANCELLED, "工具调用被取消", collected)

        return self._finish(
            run,
            RunStatus.MAX_ITERATIONS,
            f"达到 ReAct 迭代上限 {run.definition.max_react_iterations}",
            collected,
        )

    def _finish(
        self, run: AgentRun, status: RunStatus, summary: str, tool_runs: Sequence[ToolRun]
    ) -> AgentRunResult:
        run.status = status
        run.history.end_turn(str(status))
        result = AgentRunResult(
            run_id=run.run_id,
            status=status,
            summary=summary,
            files=_files_from(tool_runs),
            tests=_tests_from(tool_runs),
            evidence_refs=tuple(
                tr.result.evidence for tr in tool_runs if tr.result and tr.result.evidence
            ),
            error=None if status is RunStatus.SUCCESS else summary,
            iterations=run.context.react_iteration,
        )
        self._events.append_nowait(
            AgentEvent(
                type=EventType.AGENT_RUN_FINISHED,
                session_id=run.session_id,
                agent_run_id=run.run_id,
                payload={
                    "status": str(status),
                    "iterations": result.iterations,
                    "tool_runs": len(tool_runs),
                },
            )
        )
        return result


def _to_block(result: ToolResult) -> ToolResultBlock:
    return ToolResultBlock(
        tool_use_id=result.call_id,
        content=result.content,
        is_error=result.is_error,
        artifact_uri=result.artifact_uri,
        truncated=result.truncated,
    )


def _files_from(tool_runs: Sequence[ToolRun]) -> tuple[FileState, ...]:
    out: list[FileState] = []
    for tr in tool_runs:
        if tr.call.name != "write_file" or tr.result is None or tr.result.is_error:
            continue
        path = str(tr.result.metadata.get("path") or tr.call.arguments.get("path") or "")
        if not path:
            continue
        existed = bool(tr.result.metadata.get("existed"))
        out.append(FileState(path, FileChangeKind.MODIFIED if existed else FileChangeKind.CREATED))
    return tuple(out)


def _tests_from(tool_runs: Sequence[ToolRun]) -> tuple[TestState, ...]:
    out: list[TestState] = []
    for tr in tool_runs:
        if tr.call.name != "run_command" or tr.result is None:
            continue
        counts = extract_test_counts(tr.result.content)
        if not counts:
            continue
        failed = counts.get("failed", counts.get("failures", 0))
        outcome = TestOutcome.FAIL if failed else TestOutcome.PASS
        if tr.result.status is ToolResultStatus.TIMEOUT:
            outcome = TestOutcome.UNKNOWN
        out.append(
            TestState(
                name=str(tr.result.metadata.get("command") or "run_command"),
                outcome=outcome,
                detail=" ".join(f"{k}={v}" for k, v in counts.items()),
            )
        )
    return tuple(out)
