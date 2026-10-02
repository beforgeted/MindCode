"""Run-scoped sandbox lifecycle, including reflection and verified publication."""
from __future__ import annotations

import asyncio
from types import TracebackType

from codeagent.agent.run import AgentRun
from codeagent.execution.models import ExecutionPurpose, SandboxHandle
from codeagent.execution.podman import PodmanSandboxManager
from codeagent.execution.snapshot import TreeSnapshot
from codeagent.execution.workspace import capture_workspace, publish_workspace
from codeagent.tool.executor import SandboxExecutor
from codeagent.tool.sandbox import SandboxTools


class WorkerSandbox:
    def __init__(
        self, manager: PodmanSandboxManager | None, run: AgentRun,
        purpose: ExecutionPurpose = ExecutionPurpose.WORKER,
    ):
        self.manager, self.run = manager, run
        self.purpose = purpose
        self.handle: SandboxHandle | None = None
        self.initial: TreeSnapshot | None = None

    async def __aenter__(self) -> WorkerSandbox:
        if self.manager is not None:
            self.run.cancellation.raise_if_cancelled()
            self.initial = capture_workspace(self.run.workspace, self.manager.snapshot_limits)
            self.handle = await self.manager.open(self.initial, self.purpose)
            self.run.sandbox = SandboxTools(SandboxExecutor(
                self.manager, self.handle, self.run.workspace.root,
            ))
        return self

    async def publish(self) -> None:
        if self.manager is not None and self.handle is not None and self.initial is not None:
            self.run.cancellation.raise_if_cancelled()
            snapshot = await self.manager.seal(self.handle)
            self.run.cancellation.raise_if_cancelled()
            # Synchronous bounded IO: cancellation cannot leave a background writer
            # racing with MasterRuntime's subsequent worktree cleanup/integration.
            publish_workspace(
                self.run.workspace, self.initial, snapshot, self.manager.snapshot_limits,
            )

    async def __aexit__(
        self, exc_type: type[BaseException] | None, exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if self.manager is not None and self.handle is not None:
                await asyncio.shield(self.manager.close(self.handle))
        finally:
            self.run.sandbox = None
