"""run_command：流式落 artifact + 有界返回。

这条路径是 P1 的核心：完整输出必须在 artifact 里、内存和返回值都必须有界。
"""

from __future__ import annotations

import sys
from dataclasses import replace

from codeagent.context.token_estimator import HeuristicTokenEstimator, estimate_text
from codeagent.infra.cancellation import CancellationToken
from codeagent.tool.builtin.run_command import RunCommandTool
from codeagent.tool.execution_manager import ExecutionScope, ToolExecutionManager
from codeagent.tool.models import ToolCall, ToolResultStatus
from codeagent.tool.normalizer import ToolResultNormalizer
from codeagent.tool.registry import ToolRegistry
from codeagent.workspace.context import WorkspaceContext

PY = sys.executable.replace("\\", "/")


def _manager(artifact_store) -> ToolExecutionManager:
    estimator = HeuristicTokenEstimator()
    return ToolExecutionManager(
        registry=ToolRegistry([RunCommandTool()]),
        normalizer=ToolResultNormalizer(estimator=estimator, artifact_store=artifact_store),
        artifact_store=artifact_store,
    )


def _scope(workspace, profile) -> ExecutionScope:
    return ExecutionScope(
        agent_run_id="run_1",
        session_id="ses_1",
        workspace=WorkspaceContext.local(workspace),
        cancellation=CancellationToken(),
        profile=profile,
        turn_id="turn_1",
    )


async def test_success_and_exit_code(artifact_store, workspace, profile):
    manager = _manager(artifact_store)
    call = ToolCall("tu_1", "run_command", {"command": f'"{PY}" -c "print(\'hello agent\')"'})
    outcome = await manager.execute_batch(_scope(workspace, profile), [call])

    result = outcome.results[0]
    assert result.status is ToolResultStatus.OK
    assert result.exit_code == 0
    assert "hello agent" in result.content
    assert result.artifact is not None


async def test_nonzero_exit_is_error_result(artifact_store, workspace, profile):
    manager = _manager(artifact_store)
    call = ToolCall(
        "tu_1", "run_command", {"command": f'"{PY}" -c "import sys; sys.exit(3)"'}
    )
    outcome = await manager.execute_batch(_scope(workspace, profile), [call])

    result = outcome.results[0]
    assert result.status is ToolResultStatus.ERROR
    assert result.exit_code == 3


async def test_large_output_is_bounded_but_fully_stored(artifact_store, workspace, profile):
    manager = _manager(artifact_store)
    script = "for i in range(60000): print('line %d padding padding padding' % i)"
    call = ToolCall("tu_1", "run_command", {"command": f'"{PY}" -c "{script}"'})
    outcome = await manager.execute_batch(_scope(workspace, profile), [call])

    result = outcome.results[0]
    assert result.raw_bytes > 1_000_000
    # 返回给 LLM 的部分必须有界
    assert estimate_text(result.content) <= profile.max_tool_result_tokens * 1.5
    assert result.truncated
    # 完整输出在 artifact 里，可以 JIT 回读
    assert result.artifact is not None
    full = await artifact_store.load_text(result.artifact.uri)
    assert "line 0 padding" in full
    assert "line 59999 padding" in full


async def test_output_cap_stops_writing_artifact(artifact_store, workspace, profile):
    """超过 max_tool_output_bytes 后不再往 artifact 写，且明确标注。"""
    manager = _manager(artifact_store)
    capped = replace(profile, max_tool_output_bytes=50_000)
    script = "for i in range(40000): print('x' * 40)"
    call = ToolCall("tu_1", "run_command", {"command": f'"{PY}" -c "{script}"'})
    outcome = await manager.execute_batch(_scope(workspace, capped), [call])

    result = outcome.results[0]
    assert result.raw_bytes > 50_000
    assert result.artifact is not None
    assert result.artifact.size_bytes <= 50_000 + 65_536  # 按 chunk 粒度收敛


async def test_denied_command_is_rejected(artifact_store, workspace, profile):
    manager = _manager(artifact_store)
    call = ToolCall("tu_1", "run_command", {"command": "rm -rf /"})
    outcome = await manager.execute_batch(_scope(workspace, profile), [call])

    result = outcome.results[0]
    assert result.status is ToolResultStatus.ERROR
    assert "拒绝执行" in result.content
