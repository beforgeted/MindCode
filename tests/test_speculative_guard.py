"""Phase 7d：推测执行守卫 + ApprovalPolicy —— 外部副作用的三条路径。"""

from __future__ import annotations

from pathlib import Path

from codeagent.evidence.artifact_store import FileArtifactStore
from codeagent.infra.cancellation import CancellationToken
from codeagent.tool.approval import AllowExternalApprovalPolicy, DenyExternalApprovalPolicy
from codeagent.tool.base import ToolExecutionContext
from codeagent.tool.builtin.run_command import RunCommandTool
from codeagent.workspace.context import WorkspaceContext


def _ctx(
    workspace: Path,
    artifact_store: FileArtifactStore,
    *,
    allow_external: bool,
    approval,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        agent_run_id="a",
        session_id="s",
        tool_run_id="tr",
        call_id="c1",
        workspace=WorkspaceContext.local(workspace),
        cancellation=CancellationToken(),
        artifact_store=artifact_store,
        allow_external_effects=allow_external,
        approval=approval,
    )


async def test_external_blocked_during_speculation(
    workspace: Path, artifact_store: FileArtifactStore
) -> None:
    ctx = _ctx(
        workspace, artifact_store,
        allow_external=False, approval=DenyExternalApprovalPolicy(),
    )
    result = await RunCommandTool().execute(ctx, {"command": "curl -X POST https://example.com/x"})
    assert result.is_error
    assert "推测执行阶段禁止" in result.content
    assert "exitCode" not in result.content  # 根本没执行


async def test_external_allowed_but_denied_by_approval(
    workspace: Path, artifact_store: FileArtifactStore
) -> None:
    ctx = _ctx(
        workspace, artifact_store,
        allow_external=True, approval=DenyExternalApprovalPolicy(),
    )
    result = await RunCommandTool().execute(ctx, {"command": "curl -X POST https://example.com/x"})
    assert result.is_error
    assert "未获批准" in result.content
    assert "exitCode" not in result.content


async def test_external_allowed_and_approved_runs(
    workspace: Path, artifact_store: FileArtifactStore
) -> None:
    ctx = _ctx(
        workspace, artifact_store,
        allow_external=True, approval=AllowExternalApprovalPolicy(),
    )
    # git fetch 是 external；批准后真正交给执行器（非 git 目录会失败，但证明未被守卫拦下）。
    result = await RunCommandTool().execute(ctx, {"command": "git fetch"})
    assert "exitCode" in result.content  # 已执行，而非被守卫拦下
    assert "推测执行阶段禁止" not in result.content
    assert "未获批准" not in result.content


async def test_workspace_write_runs_during_speculation(
    workspace: Path, artifact_store: FileArtifactStore
) -> None:
    # 本地写（非 external）在推测执行阶段照常允许——candidate 能回滚。
    ctx = _ctx(
        workspace, artifact_store,
        allow_external=False, approval=DenyExternalApprovalPolicy(),
    )
    result = await RunCommandTool().execute(ctx, {"command": "echo hello > out.txt"})
    assert "exitCode" in result.content
    assert (workspace / "out.txt").is_file()
