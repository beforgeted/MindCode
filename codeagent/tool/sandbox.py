"""Explicit dispatch for tools allowed in a sandboxed Worker.

Only fixed builtin types may access controller evidence/memory. New/custom tools
must acquire a sandbox implementation before they can run in this mode.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from codeagent.execution.models import SandboxError
from codeagent.tool.base import Tool, ToolExecutionContext
from codeagent.tool.builtin.download_file import DownloadFileTool
from codeagent.tool.builtin.evidence_get import EvidenceGetTool
from codeagent.tool.builtin.grep import GrepTool
from codeagent.tool.builtin.memory_get import MemoryGetTool
from codeagent.tool.builtin.read_artifact import ReadArtifactTool
from codeagent.tool.builtin.read_file import ReadFileTool
from codeagent.tool.builtin.run_command import RunCommandTool
from codeagent.tool.builtin.write_file import WriteFileTool
from codeagent.tool.executor import SandboxExecutor, sandbox_result
from codeagent.tool.models import ToolCall, ToolResult


class SandboxTools:
    def __init__(self, executor: SandboxExecutor):
        self.executor = executor

    async def execute(
        self, tool: Tool, ctx: ToolExecutionContext, arguments: dict[str, Any],
    ) -> ToolResult:
        if type(tool) in (
            RunCommandTool, ReadArtifactTool, MemoryGetTool, EvidenceGetTool, DownloadFileTool,
        ):
            return await tool.execute(ctx, arguments)
        if type(tool) not in (ReadFileTool, WriteFileTool, GrepTool):
            raise SandboxError(f"工具尚未接入沙箱: {tool.name}")
        payload = json.dumps({"tool": tool.name, "arguments": arguments}).encode()
        if len(payload) > self.executor.manager.snapshot_limits.max_file_bytes:
            raise SandboxError("file tool request exceeds snapshot file limit")
        source = Path(__file__).with_name("sandbox_file_helper.py").read_text(encoding="utf-8")
        output = await self.executor.manager.execute_python(
            self.executor.handle, source, payload, cancellation=ctx.cancellation,
            max_output_bytes=ctx.max_output_bytes,
        )
        result = await sandbox_result(output, ctx.artifact_store)
        call = ToolCall(ctx.call_id, tool.name, arguments)
        factory = ToolResult.ok if result.exit_code == 0 else ToolResult.error
        return factory(
            call, result.content, exit_code=result.exit_code, artifact=result.artifact,
            raw_bytes=result.total_bytes, truncated=result.truncated,
        )
