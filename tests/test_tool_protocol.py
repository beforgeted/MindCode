"""tool 协议不变式 —— 上下文文档 §37.1 / §37.2。

这是整个骨架里最重要的一组测试。两份 Multi-Agent 文档的 `execute_batch`
（TaskGroup / CompletableFuture::join + `raise`）在这里会直接失败：
一个 tool 抛异常就会取消兄弟任务，assistant 的 N 个 tool_call
只回来不到 N 个 tool_result。
"""

from __future__ import annotations

import pytest

from codeagent.context.history.conversation_history import (
    ConversationHistory,
    ToolProtocolError,
    validate_tool_protocol,
)
from codeagent.context.profile import ContextProfile
from codeagent.context.token_estimator import HeuristicTokenEstimator
from codeagent.evidence.artifact_store import FileArtifactStore
from codeagent.infra.cancellation import CancellationToken
from codeagent.llm.message import Message, TextBlock, ToolResultBlock, ToolUseBlock
from codeagent.tool.execution_manager import ExecutionScope, ToolExecutionManager
from codeagent.tool.models import ToolCall, ToolResultStatus
from codeagent.tool.normalizer import ToolResultNormalizer
from codeagent.tool.registry import ToolRegistry
from codeagent.workspace.context import WorkspaceContext
from tests.conftest import BoomTool, EchoTool, SlowTool


def _manager(artifact_store: FileArtifactStore) -> ToolExecutionManager:
    estimator = HeuristicTokenEstimator()
    return ToolExecutionManager(
        registry=ToolRegistry([EchoTool(), BoomTool(), SlowTool()]),
        normalizer=ToolResultNormalizer(estimator=estimator, artifact_store=artifact_store),
        artifact_store=artifact_store,
        max_concurrency=4,
    )


def _scope(workspace, profile: ContextProfile) -> ExecutionScope:
    return ExecutionScope(
        agent_run_id="run_test",
        session_id="ses_test",
        workspace=WorkspaceContext.local(workspace),
        cancellation=CancellationToken(),
        profile=profile,
        turn_id="turn_1",
    )


async def test_failing_tool_does_not_lose_sibling_results(artifact_store, workspace, profile):
    manager = _manager(artifact_store)
    calls = [
        ToolCall("tu_1", "echo", {"text": "a", "size": 3}),
        ToolCall("tu_2", "boom", {}),
        ToolCall("tu_3", "echo", {"text": "b", "size": 3}),
    ]
    outcome = await manager.execute_batch(_scope(workspace, profile), calls)

    assert len(outcome.results) == 3
    assert {r.call_id for r in outcome.results} == {"tu_1", "tu_2", "tu_3"}
    by_id = {r.call_id: r for r in outcome.results}
    assert by_id["tu_1"].status is ToolResultStatus.OK
    assert by_id["tu_3"].status is ToolResultStatus.OK
    assert by_id["tu_2"].status is ToolResultStatus.ERROR
    assert "boom on purpose" in by_id["tu_2"].content


async def test_unknown_tool_becomes_error_result(artifact_store, workspace, profile):
    manager = _manager(artifact_store)
    calls = [ToolCall("tu_1", "does_not_exist", {})]
    outcome = await manager.execute_batch(_scope(workspace, profile), calls)
    assert len(outcome.results) == 1
    assert outcome.results[0].status is ToolResultStatus.ERROR
    assert "未知工具" in outcome.results[0].content


async def test_timeout_becomes_result_not_exception(artifact_store, workspace, profile):
    from dataclasses import replace

    manager = _manager(artifact_store)
    fast_timeout = replace(profile, tool_timeout_seconds=0.05)
    calls = [ToolCall("tu_1", "slow", {"seconds": 5}), ToolCall("tu_2", "echo", {"text": "ok"})]
    outcome = await manager.execute_batch(_scope(workspace, fast_timeout), calls)
    assert len(outcome.results) == 2
    by_id = {r.call_id: r for r in outcome.results}
    assert by_id["tu_1"].status is ToolResultStatus.TIMEOUT
    assert by_id["tu_2"].status is ToolResultStatus.OK


async def test_history_keeps_protocol_after_batch(artifact_store, workspace, profile):
    manager = _manager(artifact_store)
    history = ConversationHistory(session_id="ses_test", agent_run_id="run_test")
    turn_id = history.begin_turn()
    history.append(Message.user("go", turn_id=turn_id))

    calls = [
        ToolCall("tu_1", "echo", {"text": "a"}),
        ToolCall("tu_2", "boom", {}),
    ]
    history.append(
        Message.assistant(
            [ToolUseBlock(c.id, c.name, c.arguments) for c in calls], turn_id=turn_id
        )
    )
    outcome = await manager.execute_batch(_scope(workspace, profile), calls)
    history.append(
        Message.tool(
            [
                ToolResultBlock(r.call_id, r.content, is_error=r.is_error)
                for r in outcome.results
            ],
            turn_id=turn_id,
        )
    )

    validate_tool_protocol(history.messages)


def test_validator_catches_orphans():
    orphan_use = [Message.assistant([ToolUseBlock("tu_x", "echo", {})])]
    with pytest.raises(ToolProtocolError):
        validate_tool_protocol(orphan_use)

    orphan_result = [Message.tool([ToolResultBlock("tu_y", "content")])]
    with pytest.raises(ToolProtocolError):
        validate_tool_protocol(orphan_result)

    ok = [
        Message.assistant([TextBlock("t"), ToolUseBlock("tu_z", "echo", {})]),
        Message.tool([ToolResultBlock("tu_z", "done")]),
    ]
    validate_tool_protocol(ok)
